package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestSavedKeyUsesWindowsDPAPIAndCanBeReplaced(t *testing.T) {
	path := filepath.Join(t.TempDir(), "profile", "agent.json")
	settings := validSettings()
	for _, key := range []string{"first-secret-key", "replacement-secret-key"} {
		settings.APIKey = key
		if err := saveAgentSettings(path, settings); err != nil {
			t.Fatal(err)
		}
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var fields map[string]any
		if err := json.Unmarshal(data, &fields); err != nil {
			t.Fatal(err)
		}
		if bytes.Contains(data, []byte(key)) || fields["api_key"] != nil || fields["api_key_protected"] == nil {
			t.Fatal("configuration did not protect the key")
		}
		saved, exists, err := loadAgentSettings(path)
		if err != nil || !exists || saved.APIKey != "" || saved.WorkerID != settings.WorkerID {
			t.Fatal("saved config could not be loaded", err)
		}
		decrypted, err := unprotectAPIKey(saved.ProtectedKey)
		if err != nil || string(decrypted) != key {
			t.Fatal("DPAPI roundtrip failed", err)
		}
	}
	files, err := filepath.Glob(filepath.Join(filepath.Dir(path), ".agent-*.tmp"))
	if err != nil || len(files) != 0 {
		t.Fatal("temporary credential files left behind", err)
	}
	if _, err := unprotectAPIKey([]byte("invalid-ciphertext")); err == nil {
		t.Fatal("corrupt DPAPI data accepted")
	}
}

func TestStartupSavedConfigAndExplicitOverridesWithoutConsole(t *testing.T) {
	t.Setenv("APPDATA", t.TempDir())
	for _, key := range []string{
		"ONGMA_SERVER", "ONGMA_KEY", "ONGMA_WORKER", "ONGMA_WORKDIR",
		"PYAUTO_SERVER", "PYAUTO_KEY", "PYAUTO_WORKER", "PYAUTO_WORKDIR",
	} {
		t.Setenv(key, "")
	}
	saved := validSettings()
	path := filepath.Join(os.Getenv("APPDATA"), agentConfigDirectory, "agent.json")
	if err := saveAgentSettings(path, saved); err != nil {
		t.Fatal(err)
	}
	input, err := os.Open(os.DevNull)
	if err != nil {
		t.Fatal(err)
	}
	defer input.Close()
	settings, err := startupAgent(nil, input, &bytes.Buffer{})
	if err != nil || settings.APIKey != saved.APIKey || settings.ServerURL != saved.ServerURL {
		t.Fatal("startup did not reuse saved config", err)
	}
	settings, err = startupAgent([]string{"-key", "override", "-server", " http://other:8000/ "}, input, &bytes.Buffer{})
	if err != nil || settings.APIKey != "override" || settings.ServerURL != "http://other:8000" {
		t.Fatal("CLI overrides were not applied or normalized", err)
	}
	unchanged, _, err := loadAgentSettings(path)
	if err != nil || unchanged.ServerURL != saved.ServerURL {
		t.Fatal("CLI invocation changed saved configuration", err)
	}
}
