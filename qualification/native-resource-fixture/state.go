package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// fixtureState is the plugin's only mutable state. effects counts real native
// effects; hits counts every handler invocation except the state read itself.
type fixtureState struct {
	mu            sync.Mutex
	nonce         string
	effects       int64
	hits          map[string]int64
	registrations int64
	configSHA     string
	logPath       string
	logOK         bool
}

type stateView struct {
	PluginID      string           `json:"plugin_id"`
	Version       string           `json:"version"`
	Generation    string           `json:"generation"`
	InstanceNonce string           `json:"instance_nonce"`
	Effects       int64            `json:"effects"`
	Hits          map[string]int64 `json:"hits"`
	Registrations int64            `json:"registrations"`
	ConfigSHA256  string           `json:"config_sha256"`
	LogPath       string           `json:"log_path"`
	LogOK         bool             `json:"log_ok"`
}

var state = &fixtureState{hits: map[string]int64{}, nonce: newNonce()}

func newNonce() string {
	var raw [8]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return fmt.Sprintf("%016x", time.Now().UnixNano())
	}
	return hex.EncodeToString(raw[:])
}

// configure records one plugin.register/plugin.reconfigure call. The digest of
// the delivered plugin configuration changes whenever any plugin option does.
func (s *fixtureState) configure(configYAML []byte) {
	sum := sha256.Sum256(configYAML)
	path := configuredLogPath(configYAML)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.registrations++
	s.configSHA = hex.EncodeToString(sum[:])
	s.logPath = path
}

func configuredLogPath(configYAML []byte) string {
	for _, line := range strings.Split(string(configYAML), "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "effect-log:") {
			continue
		}
		value := strings.Trim(strings.TrimSpace(strings.TrimPrefix(line, "effect-log:")), "\"'")
		if value != "" {
			return value
		}
	}
	if home := os.Getenv("HOME"); home != "" {
		return filepath.Join(home, "hermes-resource-fixture-effects.jsonl")
	}
	return ""
}

// appendLogLocked writes one JSON line. Callers hold s.mu.
func (s *fixtureState) appendLogLocked(event map[string]any) {
	if s.logPath == "" {
		s.logOK = false
		return
	}
	event["generation"] = generation
	event["instance_nonce"] = s.nonce
	line, err := json.Marshal(event)
	if err != nil {
		s.logOK = false
		return
	}
	file, err := os.OpenFile(s.logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		s.logOK = false
		return
	}
	_, errWrite := file.Write(append(line, '\n'))
	errClose := file.Close()
	s.logOK = errWrite == nil && errClose == nil
}

// effect counts one real native effect and returns its sequence number.
func (s *fixtureState) effect(route, detail string) int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.effects++
	s.hits[route]++
	s.appendLogLocked(map[string]any{"event": "effect", "seq": s.effects, "route": route, "detail": detail})
	return s.effects
}

// finished marks a slow effect as completed; it is not a second effect.
func (s *fixtureState) finished(route string, seq int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.appendLogLocked(map[string]any{"event": "finished", "seq": seq, "route": route})
}

func (s *fixtureState) hit(route string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.hits[route]++
}

func (s *fixtureState) view() stateView {
	s.mu.Lock()
	defer s.mu.Unlock()
	hits := make(map[string]int64, len(s.hits))
	for route, count := range s.hits {
		hits[route] = count
	}
	return stateView{
		PluginID: pluginID, Version: pluginVersion, Generation: generation,
		InstanceNonce: s.nonce, Effects: s.effects, Hits: hits,
		Registrations: s.registrations, ConfigSHA256: s.configSHA,
		LogPath: s.logPath, LogOK: s.logOK,
	}
}

// nonceValue is immutable after construction and needs no lock.
func (s *fixtureState) nonceValue() string {
	return s.nonce
}
