package main

import (
	"encoding/json"
	"sort"
	"strings"
)

const (
	abiVersion    = 1 // sdk/pluginabi.ABIVersion
	schemaVersion = 6 // sdk/pluginabi.SchemaVersion: raw (unescaped) management bodies

	pluginID     = "hermes-resource-fixture"
	resourceBase = "/v0/resource/plugins/" + pluginID
	menuLabel    = "Hermes Resource Fixture"
)

// Set at link time (-X main.generation=2 -X main.pluginVersion=2.0.0) so a
// second build behind identical routes is distinguishable native code.
var (
	generation    = "1"
	pluginVersion = "1.0.0"
)

type lifecycleRequest struct {
	ConfigYAML []byte `json:"config_yaml"`
}

// Field names mirror sdk/pluginapi.Metadata, which carries no JSON tags.
type configField struct {
	Name        string
	Type        string
	EnumValues  []string
	Description string
}

type metadata struct {
	Name             string
	Version          string
	Author           string
	GitHubRepository string
	Logo             string
	ConfigFields     []configField
}

type capabilities struct {
	ManagementAPI bool `json:"management_api"`
}

type registration struct {
	SchemaVersion uint32       `json:"schema_version"`
	Metadata      metadata     `json:"metadata"`
	Capabilities  capabilities `json:"capabilities"`
}

type resourceRoute struct {
	Path        string
	Menu        string
	Description string
}

type managementRegistration struct {
	Resources []resourceRoute `json:"resources"`
}

type managementRequest struct {
	Method  string              `json:"Method"`
	Path    string              `json:"Path"`
	Headers map[string][]string `json:"Headers"`
	Query   map[string][]string `json:"Query"`
}

type response struct {
	StatusCode int                 `json:"StatusCode"`
	Headers    map[string][]string `json:"Headers"`
	Body       []byte              `json:"Body"`
}

type routeHandler func(*managementRequest) (response, error)

// dispatch maps one plugin ABI method to its envelope. A recovered panic never
// crosses the cgo boundary, where it would terminate the whole CPA process.
func dispatch(method string, request []byte) (raw []byte, failed bool) {
	defer func() {
		if recovered := recover(); recovered != nil {
			raw, failed = errorEnvelope("plugin_panic", "fixture handler panicked"), true
		}
	}()
	switch method {
	case "plugin.register", "plugin.reconfigure":
		var req lifecycleRequest
		_ = json.Unmarshal(request, &req)
		state.configure(req.ConfigYAML)
		return okEnvelope(registration{
			SchemaVersion: schemaVersion,
			Metadata: metadata{
				Name:             "Hermes native resource fixture",
				Version:          pluginVersion,
				Author:           "hermes-qualification",
				GitHubRepository: "https://github.invalid/hermes-qualification/native-resource-fixture",
				ConfigFields: []configField{
					{Name: "effect-log", Type: "string", Description: "Absolute path of the append-only JSONL effect log."},
					{Name: "note", Type: "string", Description: "Free text that changes the plugin configuration digest."},
				},
			},
			Capabilities: capabilities{ManagementAPI: true},
		})
	case "plugin.quiesce", "plugin.shutdown":
		return okEnvelope(map[string]any{})
	case "management.register":
		return okEnvelope(managementRegistration{Resources: resources()})
	case "management.handle":
		return handleManagement(request)
	default:
		return errorEnvelope("unknown_method", "unknown method: "+method), true
	}
}

// resources lists every route; only the entry page declares a menu label.
func resources() []resourceRoute {
	paths := make([]string, 0, len(routes))
	for path := range routes {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	out := make([]resourceRoute, 0, len(paths))
	for _, path := range paths {
		route := resourceRoute{Path: path}
		if path == "/panel" {
			route.Menu = menuLabel
			route.Description = "Deterministic native resource markers for Hermes qualification."
		}
		out = append(out, route)
	}
	return out
}

func handleManagement(raw []byte) ([]byte, bool) {
	var req managementRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		return errorEnvelope("invalid_request", "management request is not valid JSON"), true
	}
	handler, known := routes[strings.TrimPrefix(req.Path, resourceBase)]
	if !known || !strings.HasPrefix(req.Path, resourceBase+"/") {
		return okEnvelope(jsonResponse(404, map[string]any{"error": "fixture_route_unknown"}))
	}
	result, err := handler(&req)
	if err != nil {
		// The host turns a plugin error into its own generic 502; the handler
		// has already run, so any effect it counted really happened.
		return errorEnvelope("fixture_plugin_error", err.Error()), true
	}
	return okEnvelope(result)
}
