package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

var errSetupCancelled = errors.New("configuração cancelada")

type agentSettings struct {
	ServerURL    string `json:"server"`
	WorkerID     string `json:"worker"`
	WorkDir      string `json:"workdir"`
	Interval     int    `json:"interval"`
	APIKey       string `json:"-"`
	ProtectedKey []byte `json:"api_key_protected,omitempty"`
}

func loadAgentSettings(path string) (agentSettings, bool, error) {
	var settings agentSettings
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return settings, false, nil
	}
	if err != nil {
		return settings, false, fmt.Errorf("ler configuração: %w", err)
	}
	if len(data) > 64*1024 || json.Unmarshal(data, &settings) != nil {
		return settings, false, errors.New("arquivo de configuração inválido; remova-o e configure novamente")
	}
	return settings, true, nil
}

func saveAgentSettings(path string, settings agentSettings) error {
	protected, err := protectAPIKey([]byte(settings.APIKey))
	if err != nil {
		return fmt.Errorf("proteger chave de API: %w", err)
	}
	settings.ProtectedKey = protected
	data, err := json.MarshalIndent(settings, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".agent-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if _, err := file.Write(data); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return os.Rename(file.Name(), path)
}

func parseAgentSettings(args []string, saved agentSettings, hostname string, getenv func(string) string, output io.Writer) (agentSettings, bool, error) {
	settings := agentSettings{ServerURL: "http://localhost:8000", WorkerID: hostname, WorkDir: "./worker_data", Interval: 3}
	if saved.ServerURL != "" {
		settings = saved
	}
	for key, target := range map[string]*string{
		"PYAUTO_SERVER": &settings.ServerURL, "PYAUTO_KEY": &settings.APIKey,
		"PYAUTO_WORKER": &settings.WorkerID, "PYAUTO_WORKDIR": &settings.WorkDir,
	} {
		if value := strings.TrimSpace(getenv(key)); value != "" {
			*target = value
		}
	}
	flags := flag.NewFlagSet("pyautomation-agent", flag.ContinueOnError)
	flags.SetOutput(output)
	// Valores são aplicados depois do parse para a ajuda nunca exibir secrets.
	server := flags.String("server", "", "URL do servidor Ongma")
	key := flags.String("key", "", "Chave de API (X-API-Key)")
	worker := flags.String("worker", "", "Nome identificador desta máquina/worker")
	workdir := flags.String("workdir", "", "Diretório local para venvs e repositórios")
	interval := flags.Int("interval", 3, "Intervalo de polling em segundos")
	configure := flags.Bool("configure", false, "Abrir assistente de configuração (Windows)")
	if err := flags.Parse(args); err != nil {
		return settings, false, err
	}
	if flags.NArg() != 0 {
		return settings, false, errors.New("argumentos posicionais não são aceitos; use -help")
	}
	flags.Visit(func(f *flag.Flag) {
		switch f.Name {
		case "server":
			settings.ServerURL = *server
		case "key":
			settings.APIKey = *key
			settings.ProtectedKey = nil
		case "worker":
			settings.WorkerID = *worker
		case "workdir":
			settings.WorkDir = *workdir
		case "interval":
			settings.Interval = *interval
		}
	})
	return settings, *configure, nil
}

func validateAgentSettings(settings *agentSettings) error {
	settings.ServerURL = strings.TrimRight(strings.TrimSpace(settings.ServerURL), "/")
	settings.APIKey = strings.TrimSpace(settings.APIKey)
	settings.WorkerID = strings.TrimSpace(settings.WorkerID)
	settings.WorkDir = strings.TrimSpace(settings.WorkDir)
	u, err := url.Parse(settings.ServerURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" ||
		u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" {
		return errors.New("informe uma URL http:// ou https:// com host e porta corretos, sem credenciais, query ou fragmento")
	}
	if port := u.Port(); port != "" {
		n, err := strconv.Atoi(port)
		if err != nil || n < 1 || n > 65535 {
			return errors.New("porta do servidor deve estar entre 1 e 65535")
		}
	}
	if settings.WorkerID == "" || strings.ContainsAny(settings.WorkerID, "\r\n\x00") {
		return errors.New("nome da máquina/worker inválido")
	}
	if settings.WorkDir == "" || strings.ContainsRune(settings.WorkDir, 0) {
		return errors.New("diretório de trabalho inválido")
	}
	if strings.ContainsAny(settings.APIKey, "\r\n\x00") {
		return errors.New("chave de API inválida")
	}
	if settings.Interval < 1 || settings.Interval > 86400 {
		return errors.New("intervalo deve estar entre 1 e 86400 segundos")
	}
	return nil
}

// A verificação é somente leitura: não usa o poll, que poderia assumir uma tarefa.
func checkAgentConnection(ctx context.Context, settings agentSettings, client *http.Client) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, settings.ServerURL+"/api/automations", nil)
	if err != nil {
		return errors.New("endereço do servidor inválido")
	}
	req.Header.Set("X-API-Key", settings.APIKey)
	// Não encaminhar a chave para destinos de redirects, mesmo no mesmo host.
	probe := *client
	probe.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	resp, err := probe.Do(req)
	if err != nil {
		return errors.New("não foi possível conectar; confira endereço, porta, servidor iniciado e firewall")
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		return errors.New("chave de API recusada; use o valor de API_KEY do servidor")
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("servidor respondeu HTTP %d; confira a URL do gerenciador", resp.StatusCode)
	}
	token, err := json.NewDecoder(io.LimitReader(resp.Body, 4096)).Token()
	if err != nil || token != json.Delim('[') {
		return errors.New("resposta inesperada; confira se a URL pertence à Ongma")
	}
	return nil
}

func promptSetting(reader *bufio.Reader, output io.Writer, label, current string) (string, error) {
	if current != "" {
		fmt.Fprintf(output, "%s [%s]: ", label, current)
	} else {
		fmt.Fprintf(output, "%s: ", label)
	}
	line, err := reader.ReadString('\n')
	if err != nil {
		return "", fmt.Errorf("leitura interrompida: %w", err)
	}
	if value := strings.TrimSpace(line); value != "" {
		return value, nil
	}
	return current, nil
}

func configureAgent(reader *bufio.Reader, output io.Writer, settings agentSettings, readKey func() (string, error), check func(agentSettings) error) (agentSettings, error) {
	fmt.Fprintln(output, "\n=== Configuração do Ongma Agent ===")
	fmt.Fprintln(output, "Informe a URL usada para acessar o gerenciador (ex.: http://192.168.1.10:8001).")
	fmt.Fprintln(output, "Use localhost apenas se o gerenciador estiver neste computador.")
	for {
		var err error
		settings.ServerURL, err = promptSetting(reader, output, "URL do servidor", settings.ServerURL)
		if err != nil {
			return settings, err
		}
		fmt.Fprint(output, "Chave de API (entrada oculta")
		if settings.APIKey != "" {
			fmt.Fprint(output, "; Enter mantém a chave atual")
		}
		fmt.Fprint(output, "): ")
		key, err := readKey()
		fmt.Fprintln(output)
		if err != nil {
			return settings, err
		}
		if strings.TrimSpace(key) != "" {
			settings.APIKey = strings.TrimSpace(key)
		}
		settings.WorkerID, err = promptSetting(reader, output, "Nome da máquina/worker", settings.WorkerID)
		if err != nil {
			return settings, err
		}
		settings.WorkDir, err = promptSetting(reader, output, "Pasta de trabalho", settings.WorkDir)
		if err != nil {
			return settings, err
		}
		err = validateAgentSettings(&settings)
		if err == nil && settings.APIKey == "" {
			err = errors.New("informe a chave de API configurada no servidor")
		}
		if err == nil {
			fmt.Fprintln(output, "Verificando conexão e autenticação...")
			err = check(settings)
		}
		if err == nil {
			return settings, nil
		}
		fmt.Fprintf(output, "Não foi possível concluir: %v\n", err)
		retry, err := promptSetting(reader, output, "Tentar novamente? S/n", "S")
		if err != nil {
			return settings, err
		}
		if !strings.EqualFold(retry, "s") {
			return settings, errSetupCancelled
		}
	}
}

func startupAgent(args []string, input *os.File, output io.Writer) (agentSettings, error) {
	configDir, err := os.UserConfigDir()
	if err != nil {
		return agentSettings{}, err
	}
	path := filepath.Join(configDir, "PyAutomation", "agent.json")
	saved, exists, err := loadAgentSettings(path)
	if err != nil {
		return saved, fmt.Errorf("%s: %w", path, err)
	}
	hostname, _ := os.Hostname()
	if hostname == "" {
		hostname = "worker-01"
	}
	settings, configure, err := parseAgentSettings(args, saved, hostname, os.Getenv, output)
	if errors.Is(err, flag.ErrHelp) {
		return settings, errSetupCancelled
	}
	if err != nil {
		return settings, err
	}
	reader := bufio.NewReader(input)
	interactive := hasSetupConsole(input)
	if exists && !configure {
		if err := validateAgentSettings(&settings); err != nil {
			return settings, err
		}
	}
	if settings.APIKey == "" && len(settings.ProtectedKey) > 0 {
		key, err := unprotectAPIKey(settings.ProtectedKey)
		if err != nil {
			if !interactive {
				return settings, errors.New("não foi possível abrir a chave salva; configure novamente com -configure")
			}
			fmt.Fprintln(output, "Não foi possível abrir a chave salva neste usuário do Windows. Informe-a novamente.")
			configure = true
		} else {
			settings.APIKey = string(key)
		}
	}
	if len(args) == 0 && interactive && os.Getenv("PYAUTO_SERVER") == "" {
		if exists && !configure {
			fmt.Fprintf(output, "\nServidor: %s\nMáquina: %s\n", settings.ServerURL, settings.WorkerID)
			for {
				choice, err := promptSetting(reader, output, "Enter para iniciar, C para configurar, S para sair", "")
				if err != nil {
					return settings, err
				}
				if strings.EqualFold(choice, "s") {
					return settings, errSetupCancelled
				}
				if choice == "" || strings.EqualFold(choice, "c") {
					configure = choice != ""
					break
				}
			}
		} else if !exists {
			settings.ServerURL = ""
			settings.WorkDir = filepath.Join(filepath.Dir(path), "worker_data")
			configure = true
		}
	}
	if configure {
		if !interactive {
			return settings, errors.New("o assistente requer um console Windows; para execução automatizada use parâmetros ou PYAUTO_SERVER/PYAUTO_KEY")
		}
		settings, err = configureAgent(reader, output, settings,
			func() (string, error) { return readConsoleKey(input, reader) },
			func(s agentSettings) error {
				ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer cancel()
				return checkAgentConnection(ctx, s, &http.Client{Timeout: 10 * time.Second})
			})
		if err != nil {
			return settings, err
		}
		if err := saveAgentSettings(path, settings); err != nil {
			return settings, fmt.Errorf("salvar configuração: %w", err)
		}
		fmt.Fprintf(output, "Configuração salva em %s\nNo painel, use Máquina / Worker Alvo: %s\n", path, settings.WorkerID)
	}
	err = validateAgentSettings(&settings)
	return settings, err
}
