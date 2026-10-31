package main

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"unsafe"
)

func hasSetupConsole(input *os.File) bool {
	var mode uint32
	return syscall.GetConsoleMode(syscall.Handle(input.Fd()), &mode) == nil
}

func readConsoleKey(input *os.File, reader *bufio.Reader) (value string, err error) {
	handle := syscall.Handle(input.Fd())
	var mode uint32
	if err := syscall.GetConsoleMode(handle, &mode); err != nil {
		return "", err
	}
	setMode := syscall.NewLazyDLL("kernel32.dll").NewProc("SetConsoleMode")
	changeMode := func(mode uint32) error {
		ok, _, callErr := setMode.Call(uintptr(handle), uintptr(mode))
		if ok == 0 {
			return callErr
		}
		return nil
	}
	const enableEchoInput = 0x0004
	if err := changeMode(mode &^ enableEchoInput); err != nil {
		return "", err
	}
	defer func() {
		if restoreErr := changeMode(mode); restoreErr != nil {
			err = errors.Join(err, fmt.Errorf("restaurar console: %w", restoreErr))
		}
	}()
	line, err := reader.ReadString('\n')
	return strings.TrimSpace(line), err
}

// DPAPI vincula a chave ao usuário atual do Windows, sem chave mestra no arquivo.
func transformAPIKey(data []byte, procedure string) ([]byte, error) {
	if len(data) == 0 {
		return nil, nil
	}
	windowsDir := os.Getenv("SystemRoot")
	if !filepath.IsAbs(windowsDir) {
		return nil, errors.New("diretório do Windows indisponível")
	}
	proc := syscall.NewLazyDLL(filepath.Join(windowsDir, "System32", "crypt32.dll")).NewProc(procedure)
	if err := proc.Find(); err != nil {
		return nil, err
	}
	type dataBlob struct {
		Size uint32
		Data *byte
	}
	in := dataBlob{Size: uint32(len(data)), Data: &data[0]}
	var out dataBlob
	const cryptprotectUIForbidden = 0x1
	ok, _, err := proc.Call(uintptr(unsafe.Pointer(&in)), 0, 0, 0, 0,
		cryptprotectUIForbidden, uintptr(unsafe.Pointer(&out)))
	runtime.KeepAlive(data)
	if ok == 0 {
		return nil, fmt.Errorf("DPAPI: %w", err)
	}
	result := append([]byte(nil), unsafe.Slice(out.Data, int(out.Size))...)
	clear(unsafe.Slice(out.Data, int(out.Size)))
	if _, err := syscall.LocalFree(syscall.Handle(unsafe.Pointer(out.Data))); err != nil {
		return nil, err
	}
	return result, nil
}

func protectAPIKey(data []byte) ([]byte, error) {
	return transformAPIKey(data, "CryptProtectData")
}

func unprotectAPIKey(data []byte) ([]byte, error) {
	return transformAPIKey(data, "CryptUnprotectData")
}

func pauseOnStartupError() {
	if len(os.Args) == 1 && hasSetupConsole(os.Stdin) {
		fmt.Print("Pressione Enter para fechar...")
		_, _ = bufio.NewReader(os.Stdin).ReadString('\n')
	}
}
