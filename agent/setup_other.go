//go:build !windows

package main

import (
	"bufio"
	"errors"
	"os"
)

// O onboarding do executável Windows usa APIs nativas. Outros sistemas mantêm CLI/env.
func hasSetupConsole(*os.File) bool { return false }

func readConsoleKey(*os.File, *bufio.Reader) (string, error) {
	return "", errors.New("assistente disponível apenas no Windows")
}

func protectAPIKey([]byte) ([]byte, error) {
	return nil, errors.New("persistência de credenciais disponível apenas no Windows; use ONGMA_KEY")
}

func unprotectAPIKey([]byte) ([]byte, error) {
	return nil, errors.New("a chave salva só pode ser aberta pelo usuário Windows que a configurou")
}

func pauseOnStartupError() {}
