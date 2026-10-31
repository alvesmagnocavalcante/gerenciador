package main

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"flag"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func validSettings() agentSettings {
	return agentSettings{ServerURL: "http://localhost:8001", APIKey: "test-key", WorkerID: "pc-test", WorkDir: "./worker_data", Interval: 3}
}

func TestSettingsPrecedenceAndHelpDoesNotExposeKey(t *testing.T) {
	saved := validSettings()
	env := map[string]string{"PYAUTO_SERVER": "http://env:8001", "PYAUTO_KEY": "env-secret", "PYAUTO_WORKER": "env-worker"}
	getenv := func(key string) string { return env[key] }
	settings, configure, err := parseAgentSettings([]string{"-server", "http://flag:8001", "-interval", "5"}, saved, "hostname", getenv, io.Discard)
	if err != nil || configure || settings.ServerURL != "http://flag:8001" || settings.APIKey != "env-secret" || settings.WorkerID != "env-worker" || settings.WorkDir != saved.WorkDir || settings.Interval != 5 {
		t.Fatal("flag/env/saved precedence did not match", err)
	}
	var help bytes.Buffer
	_, _, err = parseAgentSettings([]string{"-help"}, saved, "hostname", getenv, &help)
	if !errors.Is(err, flag.ErrHelp) || strings.Contains(help.String(), "env-secret") || strings.Contains(help.String(), saved.APIKey) {
		t.Fatal("help exposed a secret or did not return help")
	}
	settings, _, err = parseAgentSettings(nil, agentSettings{}, "hostname", func(string) string { return "" }, io.Discard)
	if err != nil || settings.ServerURL != "http://localhost:8000" || settings.WorkerID != "hostname" || settings.Interval != 3 {
		t.Fatal("CLI defaults changed", err)
	}
}

func TestValidateAgentSettings(t *testing.T) {
	for _, address := range []string{"", "localhost:8001", "ftp://host", "http://", "http://user:password@host", "http://host?key=secret", "http://host#secret", "http://host:0", "http://host:65536", "http://host:abc"} {
		t.Run(address, func(t *testing.T) {
			settings := validSettings()
			settings.ServerURL = address
			if err := validateAgentSettings(&settings); err == nil {
				t.Fatal("invalid URL accepted")
			}
		})
	}
	for _, interval := range []int{0, -1, 86401} {
		settings := validSettings()
		settings.Interval = interval
		if err := validateAgentSettings(&settings); err == nil {
			t.Fatal("unsafe polling interval accepted")
		}
	}
	settings := validSettings()
	settings.ServerURL = " https://example.com/manager/ "
	if err := validateAgentSettings(&settings); err != nil || settings.ServerURL != "https://example.com/manager" {
		t.Fatal("valid URL with path prefix rejected", err)
	}
	settings.APIKey = "key\r\nInjected: value"
	if err := validateAgentSettings(&settings); err == nil {
		t.Fatal("header injection accepted")
	}
}

func TestConnectionCheckReadOnlyAndAuthentication(t *testing.T) {
	for _, tc := range []struct {
		name   string
		status int
		body   string
		ok     bool
	}{
		{"valid", 200, "[]", true},
		{"invalid key", 401, "private response", false},
		{"forbidden", 403, "private response", false},
		{"wrong path", 404, "private response", false},
		{"wrong service", 200, "<html>Login</html>", false},
		{"wrong json", 200, "{}", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet || r.URL.Path != "/manager/api/automations" || r.Header.Get("X-API-Key") != "test-key" {
					t.Error("probe did not use a read-only authenticated API request")
				}
				w.WriteHeader(tc.status)
				_, _ = io.WriteString(w, tc.body)
			}))
			defer server.Close()
			settings := validSettings()
			settings.ServerURL = server.URL + "/manager"
			err := checkAgentConnection(context.Background(), settings, server.Client())
			if (err == nil) != tc.ok {
				t.Fatalf("unexpected check result: %v", err)
			}
			if err != nil && (strings.Contains(err.Error(), "private response") || strings.Contains(err.Error(), settings.APIKey)) {
				t.Fatal("check exposed server output or API key")
			}
		})
	}
}

func TestConnectionCheckDoesNotFollowRedirect(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/redirected" {
			t.Error("redirect was followed with a credential")
		}
		http.Redirect(w, r, "/redirected", http.StatusFound)
	}))
	defer server.Close()
	settings := validSettings()
	settings.ServerURL = server.URL
	if err := checkAgentConnection(context.Background(), settings, server.Client()); err == nil {
		t.Fatal("redirect accepted")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := checkAgentConnection(ctx, settings, &http.Client{Timeout: time.Second}); err == nil {
		t.Fatal("cancelled connection accepted")
	}
}

func TestWizardRetriesAndKeepsKeyOutOfOutput(t *testing.T) {
	// First URL is malformed, then authentication fails, then the corrected URL works.
	reader := bufio.NewReader(strings.NewReader("bad-url\n\n\nS\nhttp://server:8001\n\n\nS\n\nnew-worker\n\n"))
	var output bytes.Buffer
	checks := 0
	settings, err := configureAgent(reader, &output, validSettings(),
		func() (string, error) { return "secret-never-print", nil },
		func(settings agentSettings) error {
			checks++
			if checks == 1 {
				return errors.New("chave recusada")
			}
			if settings.WorkerID != "new-worker" || settings.ServerURL != "http://server:8001" || settings.APIKey != "secret-never-print" {
				t.Error("corrected wizard values were not preserved")
			}
			return nil
		})
	if err != nil || settings.WorkerID != "new-worker" || checks != 2 {
		t.Fatal("wizard retry failed", err)
	}
	if strings.Contains(output.String(), "secret-never-print") || strings.Contains(output.String(), "test-key") {
		t.Fatal("wizard printed an API key")
	}
}

func TestWizardCancellationAndEOF(t *testing.T) {
	for _, tc := range []struct {
		input string
		want  error
	}{
		{"\n\n\nn\n", errSetupCancelled},
		{"", io.EOF},
	} {
		_, err := configureAgent(bufio.NewReader(strings.NewReader(tc.input)), io.Discard, validSettings(),
			func() (string, error) { return "", nil },
			func(agentSettings) error { return errors.New("connection refused") })
		if !errors.Is(err, tc.want) {
			t.Fatalf("expected %v, got %v", tc.want, err)
		}
	}
}

func TestLoadSettingsRejectsCorruptFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.json")
	if _, exists, err := loadAgentSettings(path); err != nil || exists {
		t.Fatal("missing config should be allowed", err)
	}
	if err := os.WriteFile(path, []byte("{broken"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := loadAgentSettings(path); err == nil {
		t.Fatal("corrupt config accepted")
	}
}
