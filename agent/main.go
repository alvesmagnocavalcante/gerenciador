package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"
)

type Job struct {
	ExecutionID        int               `json:"execution_id"`
	AutomationID       int               `json:"automation_id"`
	Name               string            `json:"name"`
	SourceType         string            `json:"source_type"`
	GitURL             *string           `json:"git_url"`
	GitBranch          string            `json:"git_branch"`
	GitToken           *string           `json:"git_token"`
	Entrypoint         string            `json:"entrypoint"`
	Arguments          string            `json:"arguments"`
	LocalScriptContent *string           `json:"local_script_content"`
	EnvVars            map[string]string `json:"env_vars"`
	TimeoutSeconds     int               `json:"timeout_seconds"`
	MaxRetries         int               `json:"max_retries"`
	RetryDelaySeconds  int               `json:"retry_delay_seconds"`
	CleanupMode        string            `json:"cleanup_mode"`
	MaxLogSizeBytes    int               `json:"max_log_size_bytes"`
	HttpURL            *string           `json:"http_url"`
	HttpMethod         string            `json:"http_method"`
	HttpHeaders        *string           `json:"http_headers"`
	HttpBody           *string           `json:"http_body"`
	RunTrailSource     string            `json:"runtrail_runtime_source"`
}

type PollResponse struct {
	Job *Job `json:"job"`
}

type HeartbeatPayload struct {
	Logs       *string `json:"logs,omitempty"`
	ProcessPID *int    `json:"process_pid,omitempty"`
	RetryCount *int    `json:"retry_count,omitempty"`
}

type HeartbeatResponse struct {
	OK              bool `json:"ok"`
	CancelRequested bool `json:"cancel_requested"`
}

type CompletePayload struct {
	Status       string  `json:"status"`
	ExitCode     *int    `json:"exit_code,omitempty"`
	Logs         string  `json:"logs"`
	ErrorMessage *string `json:"error_message,omitempty"`
	CommitSHA    *string `json:"commit_sha,omitempty"`
	RetryCount   int     `json:"retry_count"`
}

type safeBuffer struct {
	mu                  sync.Mutex
	buf                 bytes.Buffer
	limit               int
	progressPending     []byte
	latestProgressEvent string
	runTrailEvents      []string
}

func (s *safeBuffer) Write(p []byte) (n int, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.trackProgress(p)
	limit := s.limit
	if limit <= 0 {
		limit = 5 * 1024 * 1024
	}
	n = len(p)
	remaining := limit - s.buf.Len()
	if len(p) <= remaining {
		_, err = s.buf.Write(p)
	} else {
		marker := []byte("\n[LOG TRUNCADO: LIMITE EXCEDIDO]")
		keep := limit - len(marker)
		if keep < 0 {
			keep = 0
		}
		if s.buf.Len() > keep {
			s.buf.Truncate(keep)
		}
		if s.buf.Len() < keep {
			_, _ = s.buf.Write(p[:keep-s.buf.Len()])
		}
		_, _ = s.buf.Write(marker[:min(len(marker), limit-s.buf.Len())])
	}
	return n, err
}

func (s *safeBuffer) trackProgress(p []byte) {
	data := append(s.progressPending, p...)
	lines := bytes.Split(data, []byte("\n"))
	s.progressPending = append(s.progressPending[:0], lines[len(lines)-1]...)
	if len(s.progressPending) > 4096 {
		// Uma linha comum sem quebra não pode fazer o rastreador crescer sem limite.
		s.progressPending = s.progressPending[:0]
	}
	for _, rawLine := range lines[:len(lines)-1] {
		line := bytes.TrimSuffix(rawLine, []byte("\r"))
		const runTrailPrefix = "RUNTRAIL_EVENT:"
		if bytes.HasPrefix(line, []byte(runTrailPrefix)) {
			if len(line) <= 4096 {
				s.runTrailEvents = append(s.runTrailEvents, string(line))
				if len(s.runTrailEvents) > 1000 {
					s.runTrailEvents = s.runTrailEvents[len(s.runTrailEvents)-1000:]
				}
			}
			continue
		}
		const prefix = "PYAUTO_PROGRESS:"
		if !bytes.HasPrefix(line, []byte(prefix)) {
			continue
		}
		var event struct {
			Percent *float64 `json:"percent"`
			Message any      `json:"message"`
		}
		if err := json.Unmarshal(line[len(prefix):], &event); err != nil || event.Percent == nil ||
			*event.Percent < 0 || *event.Percent > 100 || math.Trunc(*event.Percent) != *event.Percent {
			continue
		}
		if event.Message != nil {
			if _, ok := event.Message.(string); !ok {
				continue
			}
		}
		s.latestProgressEvent = string(line)
	}
}

func (s *safeBuffer) WriteString(str string) {
	_, _ = s.Write([]byte(str))
}

func (s *safeBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	result := strings.ToValidUTF8(s.buf.String(), "?")
	if s.latestProgressEvent != "" {
		result += "\n" + s.latestProgressEvent + "\n"
	}
	if len(s.runTrailEvents) > 0 {
		result += "\n" + strings.Join(s.runTrailEvents, "\n") + "\n"
	}
	return result
}

type AgentConfig struct {
	ServerURL string
	APIKey    string
	WorkerID  string
	WorkDir   string
	Interval  time.Duration
	Client    *http.Client
}

func getEnvOrDefault(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

func main() {
	hostname, _ := os.Hostname()
	if hostname == "" {
		hostname = "worker-01"
	}

	serverFlag := flag.String("server", getEnvOrDefault("PYAUTO_SERVER", "http://localhost:8000"), "URL do servidor PyAutomation Manager")
	keyFlag := flag.String("key", getEnvOrDefault("PYAUTO_KEY", ""), "Chave de API (X-API-Key)")
	workerFlag := flag.String("worker", getEnvOrDefault("PYAUTO_WORKER", hostname), "Nome identificador desta máquina/worker")
	workDirFlag := flag.String("workdir", getEnvOrDefault("PYAUTO_WORKDIR", "./worker_data"), "Diretório local para venvs e repositórios")
	intervalFlag := flag.Int("interval", 3, "Intervalo de polling em segundos")
	flag.Parse()

	absWorkDir, err := filepath.Abs(*workDirFlag)
	if err != nil {
		log.Fatalf("Erro ao resolver diretório de trabalho: %v", err)
	}
	if err := os.MkdirAll(absWorkDir, 0755); err != nil {
		log.Fatalf("Erro ao criar diretório de trabalho %s: %v", absWorkDir, err)
	}

	cfg := &AgentConfig{
		ServerURL: strings.TrimRight(strings.TrimSpace(*serverFlag), "/"),
		APIKey:    strings.TrimSpace(*keyFlag),
		WorkerID:  strings.TrimSpace(*workerFlag),
		WorkDir:   absWorkDir,
		Interval:  time.Duration(*intervalFlag) * time.Second,
		Client:    &http.Client{Timeout: 30 * time.Second},
	}

	log.Printf("====================================================")
	log.Printf("PyAutomation Remote Agent (Go) iniciado!")
	log.Printf("Worker ID : %s", cfg.WorkerID)
	log.Printf("Servidor  : %s", cfg.ServerURL)
	log.Printf("WorkDir   : %s", cfg.WorkDir)
	log.Printf("====================================================")

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	ticker := time.NewTicker(cfg.Interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			log.Println("Encerrando agente remoto graciosamente...")
			return
		case <-ticker.C:
			job, err := pollNextJob(ctx, cfg)
			if err != nil {
				log.Printf("[AVISO] Falha ao consultar servidor: %v", err)
				continue
			}
			if job != nil {
				log.Printf("[NOVA TAREFA] Recebida execução #%d da automação '%s' (ID %d)", job.ExecutionID, job.Name, job.AutomationID)
				processJob(ctx, cfg, job)
			}
		}
	}
}

func pollNextJob(ctx context.Context, cfg *AgentConfig) (*Job, error) {
	u := fmt.Sprintf("%s/api/worker/poll?worker_name=%s", cfg.ServerURL, url.QueryEscape(cfg.WorkerID))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, nil)
	if err != nil {
		return nil, err
	}
	if cfg.APIKey != "" {
		req.Header.Set("X-API-Key", cfg.APIKey)
	}

	resp, err := cfg.Client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return nil, fmt.Errorf("status HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	var pr PollResponse
	if err := json.NewDecoder(resp.Body).Decode(&pr); err != nil {
		return nil, err
	}
	return pr.Job, nil
}

func sendHeartbeat(cfg *AgentConfig, execID int, logs string, pid *int, retryCount int) bool {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	u := fmt.Sprintf("%s/api/worker/executions/%d/heartbeat", cfg.ServerURL, execID)
	payload := HeartbeatPayload{
		ProcessPID: pid,
	}
	if logs != "" {
		payload.Logs = &logs
	}
	if retryCount >= 0 {
		payload.RetryCount = &retryCount
	}
	b, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, bytes.NewReader(b))
	if err != nil {
		return true
	}
	req.Header.Set("Content-Type", "application/json")
	if cfg.APIKey != "" {
		req.Header.Set("X-API-Key", cfg.APIKey)
	}
	resp, err := cfg.Client.Do(req)
	if err != nil {
		// Falhar fechado: não continuar trabalho sem confirmação da reserva.
		return true
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return true
	}
	var hr HeartbeatResponse
	if err := json.NewDecoder(resp.Body).Decode(&hr); err != nil {
		return true
	}
	return hr.CancelRequested
}

func sendComplete(cfg *AgentConfig, execID int, payload CompletePayload) {
	u := fmt.Sprintf("%s/api/worker/executions/%d/complete", cfg.ServerURL, execID)
	b, _ := json.Marshal(payload)
	for attempt := 0; attempt < 3; attempt++ {
		req, err := http.NewRequest(http.MethodPost, u, bytes.NewReader(b))
		if err != nil {
			return
		}
		req.Header.Set("Content-Type", "application/json")
		if cfg.APIKey != "" {
			req.Header.Set("X-API-Key", cfg.APIKey)
		}
		resp, err := cfg.Client.Do(req)
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return
			}
		}
		time.Sleep(2 * time.Second)
	}
	log.Printf("[ERRO] Não foi possível reportar conclusão da execução #%d ao servidor.", execID)
}

func resolveSafePath(baseDir, relPath string) (string, error) {
	if strings.TrimSpace(relPath) == "" {
		return "", errors.New("caminho de arquivo vazio")
	}
	cleanBase, err := filepath.Abs(baseDir)
	if err != nil {
		return "", err
	}
	target, err := filepath.Abs(filepath.Join(cleanBase, relPath))
	if err != nil {
		return "", err
	}
	physicalBase, err := resolveExistingParent(cleanBase)
	if err != nil {
		return "", err
	}
	physicalTarget, err := resolveExistingParent(target)
	if err != nil {
		return "", err
	}
	rel, err := filepath.Rel(physicalBase, physicalTarget)
	if err != nil || strings.HasPrefix(rel, "..") || rel == "." {
		return "", errors.New("path traversal detectado: o arquivo deve estar dentro do diretório da automação")
	}
	return physicalTarget, nil
}

func resolveExistingParent(path string) (string, error) {
	resolved, err := filepath.EvalSymlinks(path)
	if err == nil {
		return resolved, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	if _, statErr := os.Lstat(path); statErr == nil {
		return "", err
	}
	parent := filepath.Dir(path)
	if parent == path {
		return "", err
	}
	resolvedParent, err := resolveExistingParent(parent)
	if err != nil {
		return "", err
	}
	return filepath.Join(resolvedParent, filepath.Base(path)), nil
}

func processJob(parentCtx context.Context, cfg *AgentConfig, job *Job) {
	ctx, cancel := context.WithCancel(parentCtx)
	defer cancel()
	if sendHeartbeat(cfg, job.ExecutionID, "", nil, -1) {
		cancel()
	}
	go func() {
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if sendHeartbeat(cfg, job.ExecutionID, "", nil, -1) {
					cancel()
					return
				}
			}
		}
	}()
	if ctx.Err() != nil {
		sendComplete(cfg, job.ExecutionID, CompletePayload{Status: "CANCELLED"})
		return
	}
	if job.SourceType == "http" {
		executeHTTPJob(ctx, cfg, job)
		return
	}

	autoDir := filepath.Join(cfg.WorkDir, fmt.Sprintf("auto_%d", job.AutomationID))
	if err := os.MkdirAll(autoDir, 0755); err != nil {
		errMsg := fmt.Sprintf("Erro ao criar pasta da automação: %v", err)
		sendComplete(cfg, job.ExecutionID, CompletePayload{
			Status:       "FAILED",
			Logs:         errMsg,
			ErrorMessage: &errMsg,
		})
		return
	}

	workingDir := autoDir
	if job.SourceType == "git" {
		workingDir = filepath.Join(autoDir, "repo")
	}
	runtimeDir := filepath.Join(autoDir, ".pyautomation_runtime")
	if job.RunTrailSource != "" {
		runTrailDir := filepath.Join(runtimeDir, "runtrail")
		if err := os.MkdirAll(runTrailDir, 0755); err != nil {
			errMsg := fmt.Sprintf("Falha ao preparar RunTrail: %v", err)
			sendComplete(cfg, job.ExecutionID, CompletePayload{Status: "FAILED", Logs: errMsg, ErrorMessage: &errMsg})
			return
		}
		if err := os.WriteFile(filepath.Join(runTrailDir, "__init__.py"), []byte(job.RunTrailSource), 0644); err != nil {
			errMsg := fmt.Sprintf("Falha ao instalar RunTrail: %v", err)
			sendComplete(cfg, job.ExecutionID, CompletePayload{Status: "FAILED", Logs: errMsg, ErrorMessage: &errMsg})
			return
		}
	}

	entrypointPath, err := resolveSafePath(workingDir, job.Entrypoint)
	if err != nil {
		errMsg := err.Error()
		sendComplete(cfg, job.ExecutionID, CompletePayload{
			Status:       "FAILED",
			Logs:         errMsg,
			ErrorMessage: &errMsg,
		})
		return
	}

	var commitSHA *string
	if job.SourceType == "git" {
		if job.GitURL == nil || strings.TrimSpace(*job.GitURL) == "" {
			errMsg := "URL do repositório Git não informada."
			sendComplete(cfg, job.ExecutionID, CompletePayload{
				Status:       "FAILED",
				Logs:         errMsg,
				ErrorMessage: &errMsg,
			})
			return
		}
		sha, err := cloneOrUpdateRepo(ctx, workingDir, *job.GitURL, job.GitBranch, job.GitToken)
		if err != nil {
			errMsg := fmt.Sprintf("Falha ao sincronizar repositório Git: %v", err)
			sendComplete(cfg, job.ExecutionID, CompletePayload{
				Status:       "FAILED",
				Logs:         errMsg,
				ErrorMessage: &errMsg,
			})
			return
		}
		commitSHA = &sha
	} else if job.SourceType == "local" && job.LocalScriptContent != nil && *job.LocalScriptContent != "" {
		if err := os.MkdirAll(filepath.Dir(entrypointPath), 0755); err == nil {
			_ = os.WriteFile(entrypointPath, []byte(*job.LocalScriptContent), 0644)
		}
	}

	entrypointPath, err = resolveSafePath(workingDir, job.Entrypoint)
	if err != nil {
		msg := err.Error()
		sendComplete(cfg, job.ExecutionID, CompletePayload{Status: "FAILED", ErrorMessage: &msg})
		return
	}
	pythonBin, err := ensureVenv(ctx, autoDir, workingDir)
	if err != nil {
		errMsg := fmt.Sprintf("Falha ao preparar virtualenv na máquina '%s': %v", cfg.WorkerID, err)
		sendComplete(cfg, job.ExecutionID, CompletePayload{
			Status:       "FAILED",
			Logs:         errMsg,
			ErrorMessage: &errMsg,
		})
		return
	}

	if _, err := os.Stat(entrypointPath); err != nil {
		errMsg := fmt.Sprintf("Arquivo principal não encontrado na máquina '%s': %s", cfg.WorkerID, job.Entrypoint)
		sendComplete(cfg, job.ExecutionID, CompletePayload{
			Status:       "FAILED",
			Logs:         errMsg,
			ErrorMessage: &errMsg,
		})
		return
	}

	// Escrever arquivo .env físico
	if len(job.EnvVars) > 0 {
		var envLines []string
		for k, v := range job.EnvVars {
			envLines = append(envLines, fmt.Sprintf("%s=%s", k, v))
		}
		_ = os.WriteFile(filepath.Join(workingDir, ".env"), []byte(strings.Join(envLines, "\n")+"\n"), 0600)
	}

	// Loop de execução com suporte a Retries
	for attempt := 0; attempt <= job.MaxRetries; attempt++ {
		status, exitCode, logs, errMsg, wasCancelled := runPythonSubprocess(ctx, cfg, job, pythonBin, entrypointPath, workingDir, runtimeDir, attempt)

		// Limpeza pós-execução (Modo Efêmero) na última tentativa ou sucesso/cancelamento
		isFinalAttempt := wasCancelled || status == "SUCCESS" || attempt >= job.MaxRetries
		if isFinalAttempt && job.SourceType == "git" {
			if job.CleanupMode == "delete_repo" {
				_ = os.RemoveAll(workingDir)
			} else if job.CleanupMode == "delete_git" {
				_ = os.RemoveAll(filepath.Join(workingDir, ".git"))
			}
		}

		if !isFinalAttempt {
			log.Printf("[RETRY] Tentativa %d/%d para execução #%d falhou (%s). Aguardando %ds...", attempt+1, job.MaxRetries, job.ExecutionID, status, job.RetryDelaySeconds)
			select {
			case <-ctx.Done():
				sendComplete(cfg, job.ExecutionID, CompletePayload{Status: "CANCELLED", Logs: logs})
				return
			case <-time.After(time.Duration(job.RetryDelaySeconds) * time.Second):
			}
			continue
		}

		sendComplete(cfg, job.ExecutionID, CompletePayload{
			Status:       status,
			ExitCode:     &exitCode,
			Logs:         logs,
			ErrorMessage: errMsg,
			CommitSHA:    commitSHA,
			RetryCount:   attempt,
		})
		log.Printf("[CONCLUÍDO] Execução #%d finalizada com status %s (exit code %d)", job.ExecutionID, status, exitCode)
		return
	}
}

func runPythonSubprocess(
	parentCtx context.Context,
	cfg *AgentConfig,
	job *Job,
	pythonBin, entrypointPath, workingDir, runtimeDir string,
	attempt int,
) (status string, exitCode int, logs string, errMsg *string, wasCancelled bool) {
	timeout := time.Duration(job.TimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = time.Hour
	}

	ctx, cancel := context.WithTimeout(parentCtx, timeout)
	defer cancel()

	args := []string{entrypointPath}
	if strings.TrimSpace(job.Arguments) != "" {
		args = append(args, splitArgs(job.Arguments)...)
	}

	cmd := exec.CommandContext(ctx, pythonBin, args...)
	cmd.Dir = workingDir

	env := os.Environ()
	pythonPath := strings.Join([]string{runtimeDir, workingDir}, string(os.PathListSeparator))
	env = append(env,
		fmt.Sprintf("PYTHONPATH=%s", pythonPath),
		fmt.Sprintf("PYAUTOMATION_EXECUTION_ID=%d", job.ExecutionID),
		fmt.Sprintf("PYAUTOMATION_RETRY_COUNT=%d", attempt),
	)
	for k, v := range job.EnvVars {
		env = append(env, fmt.Sprintf("%s=%s", k, v))
	}
	cmd.Env = append(env, "PYTHONUNBUFFERED=1", "PYTHONIOENCODING=utf-8")

	outBuf := safeBuffer{limit: job.MaxLogSizeBytes}
	outBuf.WriteString(fmt.Sprintf("[AGENTE REMOTO '%s'] Executando %s (tentativa %d)...\n", cfg.WorkerID, job.Entrypoint, attempt+1))
	cmd.Stdout = &outBuf
	cmd.Stderr = &outBuf

	if err := cmd.Start(); err != nil {
		msg := fmt.Sprintf("Erro ao iniciar processo Python: %v", err)
		outBuf.WriteString(msg + "\n")
		return "FAILED", -1, outBuf.String(), &msg, false
	}

	pid := cmd.Process.Pid
	var cancelledByServer bool
	var cancelMu sync.Mutex

	doneCh := make(chan struct{})
	go func() {
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-doneCh:
				return
			case <-ticker.C:
				if sendHeartbeat(cfg, job.ExecutionID, outBuf.String(), &pid, attempt) {
					cancelMu.Lock()
					cancelledByServer = true
					cancelMu.Unlock()
					cancel()
					return
				}
			}
		}
	}()

	err := cmd.Wait()
	close(doneCh)

	cancelMu.Lock()
	wasCancelled = cancelledByServer
	cancelMu.Unlock()

	if wasCancelled || errors.Is(parentCtx.Err(), context.Canceled) {
		msg := "Execução cancelada pelo usuário."
		outBuf.WriteString("\n[EXECUÇÃO CANCELADA PELO USUÁRIO]\n")
		return "CANCELLED", -1, outBuf.String(), &msg, true
	}

	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		msg := fmt.Sprintf("Tempo limite de %ds excedido.", job.TimeoutSeconds)
		outBuf.WriteString("\n[TIMEOUT EXCEDIDO]\n")
		return "TIMEOUT", -1, outBuf.String(), &msg, false
	}

	if err != nil {
		exitCode = 1
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			exitCode = exitErr.ExitCode()
		}
		msg := fmt.Sprintf("Processo encerrou com código de saída %d.", exitCode)
		return "FAILED", exitCode, outBuf.String(), &msg, false
	}

	return "SUCCESS", 0, outBuf.String(), nil, false
}

func executeHTTPJob(parentCtx context.Context, cfg *AgentConfig, job *Job) {
	if job.HttpURL == nil || strings.TrimSpace(*job.HttpURL) == "" {
		msg := "URL HTTP não configurada."
		sendComplete(cfg, job.ExecutionID, CompletePayload{
			Status:       "FAILED",
			Logs:         msg,
			ErrorMessage: &msg,
		})
		return
	}

	method := strings.ToUpper(strings.TrimSpace(job.HttpMethod))
	if method == "" {
		method = http.MethodGet
	}

	timeout := time.Duration(job.TimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = 60 * time.Second
	}
	ctx, cancel := context.WithTimeout(parentCtx, timeout)
	defer cancel()

	var bodyReader io.Reader
	if job.HttpBody != nil && *job.HttpBody != "" {
		bodyReader = strings.NewReader(*job.HttpBody)
	}

	req, err := http.NewRequestWithContext(ctx, method, *job.HttpURL, bodyReader)
	if err != nil {
		msg := err.Error()
		sendComplete(cfg, job.ExecutionID, CompletePayload{
			Status:       "FAILED",
			Logs:         msg,
			ErrorMessage: &msg,
		})
		return
	}

	if job.HttpHeaders != nil && strings.TrimSpace(*job.HttpHeaders) != "" {
		var hdrs map[string]string
		if err := json.Unmarshal([]byte(*job.HttpHeaders), &hdrs); err == nil {
			for k, v := range hdrs {
				req.Header.Set(k, v)
			}
		}
	}

	resp, err := (&http.Client{Timeout: timeout}).Do(req)
	if err != nil {
		status := "FAILED"
		if errors.Is(ctx.Err(), context.Canceled) {
			status = "CANCELLED"
		}
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			status = "TIMEOUT"
		}
		msg := err.Error()
		logs := fmt.Sprintf("[INÍCIO] Disparo HTTP (%s): %s %s\n[ERRO HTTP] %s", cfg.WorkerID, method, *job.HttpURL, msg)
		sendComplete(cfg, job.ExecutionID, CompletePayload{
			Status:       status,
			Logs:         logs,
			ErrorMessage: &msg,
		})
		return
	}
	defer resp.Body.Close()

	respBytes, _ := io.ReadAll(io.LimitReader(resp.Body, 5000))
	code := resp.StatusCode
	status := "FAILED"
	if code >= 200 && code < 300 {
		status = "SUCCESS"
	}
	logs := fmt.Sprintf("[INÍCIO] Disparo HTTP (%s): %s %s\n[RESPOSTA] Status: %d\n%s", cfg.WorkerID, method, *job.HttpURL, code, string(respBytes))
	sendComplete(cfg, job.ExecutionID, CompletePayload{
		Status:   status,
		ExitCode: &code,
		Logs:     logs,
	})
}

func cloneOrUpdateRepo(ctx context.Context, repoDir, gitURL, branch string, token *string) (string, error) {
	if branch == "" {
		branch = "main"
	}
	authURL := gitURL
	if token != nil && strings.TrimSpace(*token) != "" && strings.HasPrefix(gitURL, "https://") {
		authURL = strings.Replace(gitURL, "https://", fmt.Sprintf("https://%s@", strings.TrimSpace(*token)), 1)
	}

	gitDir := filepath.Join(repoDir, ".git")
	if info, err := os.Stat(gitDir); err == nil && info.IsDir() {
		_ = runGit(ctx, repoDir, "remote", "set-url", "origin", authURL)
		if err := runGit(ctx, repoDir, "fetch", "origin", branch); err == nil {
			_ = runGit(ctx, repoDir, "checkout", branch)
			if err := runGit(ctx, repoDir, "pull", "origin", branch); err == nil {
				if sha, err := getGitOutput(ctx, repoDir, "rev-parse", "HEAD"); err == nil {
					return sha, nil
				}
			}
		}
	}

	_ = os.RemoveAll(repoDir)
	if err := os.MkdirAll(repoDir, 0755); err != nil {
		return "", err
	}
	if err := runGit(ctx, "", "clone", "--branch", branch, "--depth", "1", authURL, repoDir); err != nil {
		_ = os.RemoveAll(repoDir)
		return "", err
	}
	return getGitOutput(ctx, repoDir, "rev-parse", "HEAD")
}

func runGit(ctx context.Context, cwd string, args ...string) error {
	_, err := getGitOutput(ctx, cwd, args...)
	return err
}

func getGitOutput(parentCtx context.Context, cwd string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(parentCtx, 2*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, "git", args...)
	if cwd != "" {
		cmd.Dir = cwd
	}
	cmd.Env = append(os.Environ(), "GIT_TERMINAL_PROMPT=0")
	out, err := boundedCommandOutput(cmd)
	if err != nil {
		return "", fmt.Errorf("%v: %s", err, strings.TrimSpace(string(out)))
	}
	return strings.TrimSpace(string(out)), nil
}

func ensureVenv(parentCtx context.Context, autoDir, workingDir string) (string, error) {
	ctx, cancel := context.WithTimeout(parentCtx, 5*time.Minute)
	defer cancel()
	venvDir := filepath.Join(autoDir, ".venv")
	pythonBin := filepath.Join(venvDir, "bin", "python")
	if runtime.GOOS == "windows" {
		pythonBin = filepath.Join(venvDir, "Scripts", "python.exe")
	}

	hasUv := false
	if _, err := exec.LookPath("uv"); err == nil {
		hasUv = true
	}

	venvCreated := false
	if _, err := os.Stat(pythonBin); err != nil {
		if hasUv {
			cmd := exec.CommandContext(ctx, "uv", "venv", venvDir)
			if out, err := boundedCommandOutput(cmd); err != nil {
				return "", fmt.Errorf("uv venv falhou: %v (%s)", err, string(out))
			}
		} else {
			cmd := exec.CommandContext(ctx, "python", "-m", "venv", venvDir)
			if out, err := boundedCommandOutput(cmd); err != nil {
				return "", fmt.Errorf("python -m venv falhou: %v (%s)", err, string(out))
			}
		}
		venvCreated = true
	}

	depFiles := make(map[string]string)
	for _, p := range []string{filepath.Join(workingDir, "pyproject.toml"), filepath.Join(autoDir, "pyproject.toml")} {
		if info, err := os.Stat(p); err == nil && !info.IsDir() {
			depFiles["pyproject"] = p
			break
		}
	}
	for _, p := range []string{filepath.Join(workingDir, "requirements.txt"), filepath.Join(autoDir, "requirements.txt")} {
		if info, err := os.Stat(p); err == nil && !info.IsDir() {
			depFiles["requirements"] = p
			break
		}
	}

	if len(depFiles) == 0 {
		return pythonBin, nil
	}

	hashFile := filepath.Join(venvDir, ".deps_hash")
	currentHash := computeDepsHash(depFiles)
	if !venvCreated {
		if existing, err := os.ReadFile(hashFile); err == nil && strings.TrimSpace(string(existing)) == currentHash {
			return pythonBin, nil
		}
	}

	for _, key := range []string{"pyproject", "requirements"} {
		depPath, ok := depFiles[key]
		if !ok {
			continue
		}
		var cmd *exec.Cmd
		if hasUv {
			cmd = exec.CommandContext(ctx, "uv", "pip", "install", "--python", pythonBin, "-r", depPath)
		} else {
			if key == "requirements" {
				cmd = exec.CommandContext(ctx, pythonBin, "-m", "pip", "install", "-r", depPath)
			} else {
				cmd = exec.CommandContext(ctx, pythonBin, "-m", "pip", "install", filepath.Dir(depPath))
			}
		}
		cmd.Dir = workingDir
		if out, err := boundedCommandOutput(cmd); err != nil {
			return "", fmt.Errorf("falha ao instalar dependências de %s: %v (%s)", depPath, err, string(out))
		}
	}

	if err := os.WriteFile(hashFile, []byte(currentHash), 0644); err != nil {
		return "", err
	}
	return pythonBin, nil
}

func computeDepsHash(depFiles map[string]string) string {
	h := sha256.New()
	keys := make([]string, 0, len(depFiles))
	for k := range depFiles {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		h.Write([]byte(k))
		if data, err := os.ReadFile(depFiles[k]); err == nil {
			h.Write(data)
		}
	}
	return hex.EncodeToString(h.Sum(nil))
}

func splitArgs(input string) []string {
	var args []string
	var current strings.Builder
	var inQuote rune
	for _, r := range input {
		switch {
		case inQuote != 0:
			if r == inQuote {
				inQuote = 0
			} else {
				current.WriteRune(r)
			}
		case r == '"' || r == '\'':
			inQuote = r
		case r == ' ' || r == '\t':
			if current.Len() > 0 {
				args = append(args, current.String())
				current.Reset()
			}
		default:
			current.WriteRune(r)
		}
	}
	if current.Len() > 0 {
		args = append(args, current.String())
	}
	return args
}

func boundedCommandOutput(cmd *exec.Cmd) ([]byte, error) {
	var output safeBuffer
	cmd.Stdout, cmd.Stderr = &output, &output
	err := cmd.Run()
	return []byte(output.String()), err
}
