package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func TestSafeBufferBoundedAndConcurrent(t *testing.T) {
	buf := &safeBuffer{limit: 128}
	var group sync.WaitGroup
	for i := 0; i < 4; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			text := strings.Repeat("x", 1_000_000)
			if n, err := buf.Write([]byte(text)); err != nil || n != len(text) {
				t.Errorf("Write: n=%d, err=%v", n, err)
			}
		}()
	}
	group.Wait()
	if len(buf.String()) > 128 || !strings.Contains(buf.String(), "TRUNCADO") {
		t.Fatalf("invalid bounded output: %q", buf.String())
	}
}

func TestSafeBufferSmallLimitAndInvalidUTF8(t *testing.T) {
	buf := &safeBuffer{limit: 8}
	_, _ = buf.Write([]byte{0xff, 0xfe, 'x'})
	if !json.Valid([]byte(`"` + buf.String() + `"`)) {
		t.Fatal("invalid UTF-8 output")
	}
	buf.WriteString(strings.Repeat("a", 100))
	if len(buf.String()) > 8 {
		t.Fatal("small log limit exceeded")
	}
}

func TestSafeBufferPreservesLatestProgressAfterLogTruncation(t *testing.T) {
	buf := &safeBuffer{limit: 128}
	buf.WriteString("PYAUTO_PROGRESS:{\"percent\":42,\"message\":\"Processando\"}\n")
	buf.WriteString(strings.Repeat("x", 1_000_000))
	if !strings.Contains(buf.String(), `"percent":42`) {
		t.Fatalf("latest progress event was lost: %q", buf.String())
	}
}

func TestSafeBufferPreservesRunTrailEventsAfterLogTruncation(t *testing.T) {
	buf := &safeBuffer{limit: 128}
	buf.WriteString("RUNTRAIL_EVENT:{\"event\":\"started\",\"event_id\":\"event-1\"}\n")
	buf.WriteString(strings.Repeat("x", 1_000_000))
	if !strings.Contains(buf.String(), `RUNTRAIL_EVENT:{"event":"started"`) {
		t.Fatalf("RunTrail event was lost: %q", buf.String())
	}
}

func TestResolveSafePath(t *testing.T) {
	base := t.TempDir()
	for _, path := range []string{"", "../outside.py", "nested/../../outside.py", "."} {
		if _, err := resolveSafePath(base, path); err == nil {
			t.Errorf("unsafe path accepted: %q", path)
		}
	}
	if _, err := resolveSafePath(base, "nested/main.py"); err != nil {
		t.Fatalf("safe missing path rejected: %v", err)
	}
}

func TestResolveSafePathRejectsSymlink(t *testing.T) {
	base, outside := t.TempDir(), t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "main.py"), []byte("print(1)"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(base, "link")); err != nil {
		t.Skipf("symlinks indisponíveis neste sistema: %v", err)
	}
	if _, err := resolveSafePath(base, "link/main.py"); err == nil {
		t.Fatal("symlink escape accepted")
	}
}

func TestCancelledJobDoesNotStart(t *testing.T) {
	completed := make(chan string, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if strings.HasSuffix(r.URL.Path, "/heartbeat") {
			_, _ = w.Write([]byte(`{"ok":true,"cancel_requested":true}`))
			return
		}
		var payload CompletePayload
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Error(err)
		}
		completed <- payload.Status
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()
	cfg := &AgentConfig{ServerURL: server.URL, Client: server.Client(), WorkDir: t.TempDir()}
	processJob(context.Background(), cfg, &Job{ExecutionID: 1, AutomationID: 1, SourceType: "local", Entrypoint: "main.py"})
	if status := <-completed; status != "CANCELLED" {
		t.Fatalf("unexpected completion: %s", status)
	}
	if _, err := os.Stat(filepath.Join(cfg.WorkDir, "auto_1")); !os.IsNotExist(err) {
		t.Fatal("cancelled job prepared a workspace")
	}
}

func TestLifecycleHeartbeatDoesNotOverwriteLogsOrRetry(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Error(err)
		}
		if _, ok := payload["logs"]; ok {
			t.Error("empty lifecycle heartbeat overwrote logs")
		}
		if _, ok := payload["retry_count"]; ok {
			t.Error("lifecycle heartbeat overwrote retry count")
		}
		_, _ = w.Write([]byte(`{"ok":true,"cancel_requested":false}`))
	}))
	defer server.Close()
	sendHeartbeat(&AgentConfig{ServerURL: server.URL, Client: server.Client()}, 1, "", nil, -1)
}

func TestMissingExecutionStopsAgent(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()
	if !sendHeartbeat(&AgentConfig{ServerURL: server.URL, Client: server.Client()}, 1, "", nil, -1) {
		t.Fatal("agent continued after losing its execution reservation")
	}
}
