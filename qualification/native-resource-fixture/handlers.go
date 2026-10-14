package main

import (
	"bytes"
	"compress/gzip"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"image"
	"image/color"
	"image/png"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

//go:embed assets/*
var assets embed.FS

const maxLargeBytes = 34 << 20 // above the owner's 32 MiB resource response cap

var routes = map[string]routeHandler{}

func init() {
	script := "application/javascript; charset=utf-8"
	routes["/panel"] = panelHandler
	routes["/app.js"] = staticHandler("/app.js", "app.js", script, false)
	routes["/module.js"] = staticHandler("/module.js", "module.js", script, false)
	routes["/static-dep.js"] = staticHandler("/static-dep.js", "static-dep.js", script, false)
	routes["/dynamic.js"] = staticHandler("/dynamic.js", "dynamic.js", script, false)
	routes["/sw.js"] = staticHandler("/sw.js", "sw.js", script, false)
	routes["/gzip-asset.js"] = staticHandler("/gzip-asset.js", "gzip-asset.js", script, true)
	routes["/gzip-module.js"] = staticHandler("/gzip-module.js", "gzip-module.js", script, true)
	routes["/style.css"] = staticHandler("/style.css", "style.css", "text/css; charset=utf-8", false)
	routes["/logo.png"] = logoHandler
	routes["/form"] = formHandler
	routes["/api/state"] = stateHandler
	routes["/api/effect"] = effectHandler
	routes["/api/query"] = queryHandler
	routes["/api/headers"] = headersHandler
	routes["/api/forge"] = forgeHandler
	routes["/api/binary"] = binaryHandler
	routes["/api/gzip"] = gzipJSONHandler
	routes["/api/large"] = largeHandler
	routes["/api/fail"] = failHandler
	routes["/api/missing"] = missingHandler
	routes["/api/error"] = errorHandler
	routes["/api/slow"] = slowHandler
	routes["/redirect/start"] = redirectHandler("/redirect/start", "target?hop=1&dup=a&dup=b", false)
	routes["/redirect/absolute"] = redirectHandler("/redirect/absolute", resourceBase+"/redirect/target?hop=2", false)
	routes["/redirect/dotdot"] = redirectHandler("/redirect/dotdot", "../redirect/target?hop=3", false)
	routes["/redirect/effect-start"] = redirectHandler("/redirect/effect-start", "target?hop=effect", true)
	routes["/redirect/target"] = redirectTargetHandler
	routes["/redirect/outside"] = redirectHandler("/redirect/outside", "https://outside.invalid/hermes-fixture-redirect", false)
	routes["/redirect/protocol-relative"] = redirectHandler("/redirect/protocol-relative", "//outside.invalid/hermes-fixture-redirect", false)
	routes["/redirect/unregistered"] = redirectHandler("/redirect/unregistered", resourceBase+"/redirect/not-registered", false)
}

func jsonResponse(status int, value any) response {
	body, err := json.Marshal(value)
	if err != nil {
		status, body = 500, []byte(`{"error":"fixture_encode_failed"}`)
	}
	return response{StatusCode: status, Headers: map[string][]string{
		"Content-Type": {"application/json; charset=utf-8"},
	}, Body: body}
}

func first(values map[string][]string, key string) string {
	if items := values[key]; len(items) > 0 {
		return items[0]
	}
	return ""
}

func sortedKeys(values map[string][]string) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func intParam(req *managementRequest, key string, fallback, low, high int) int {
	value, err := strconv.Atoi(first(req.Query, key))
	if err != nil {
		return fallback
	}
	if value < low {
		return low
	}
	if value > high {
		return high
	}
	return value
}

func renderAsset(name string) ([]byte, error) {
	raw, err := assets.ReadFile("assets/" + name)
	if err != nil {
		return nil, err
	}
	replacer := strings.NewReplacer(
		"__BASE__", resourceBase, "__PLUGIN_ID__", pluginID,
		"__GENERATION__", generation, "__VERSION__", pluginVersion)
	return []byte(replacer.Replace(string(raw))), nil
}

func gzipBytes(raw []byte) ([]byte, error) {
	var buffer bytes.Buffer
	writer, err := gzip.NewWriterLevel(&buffer, gzip.BestCompression)
	if err != nil {
		return nil, err
	}
	if _, err = writer.Write(raw); err != nil {
		return nil, err
	}
	if err = writer.Close(); err != nil {
		return nil, err
	}
	return buffer.Bytes(), nil
}

// hostileHeaders are response headers a plugin must never be able to use
// against the host: forged host-owned markers, credentials, navigation, framing
// and policy. A correct owner and presentation host drop every one of them.
func hostileHeaders(challenge bool) map[string][]string {
	headers := map[string][]string{
		"X-Hermes-Resource-Invoked":        {"0", "0"},
		"X-Hermes-Resource-Revision":       {"none-0"},
		"X-Hermes-Config-Revision":         {strings.Repeat("0", 64)},
		"X-Hermes-Egress-Policy":           {"forged"},
		"X-Hermes-Plugin-Scope":            {"forged"},
		"Set-Cookie":                       {"hermes_fixture_forged=1; Path=/"},
		"Refresh":                          {"0; url=https://outside.invalid/hermes-fixture-refresh"},
		"Content-Security-Policy":          {"default-src * 'unsafe-inline' 'unsafe-eval' data: blob:"},
		"X-Frame-Options":                  {"DENY"},
		"Access-Control-Allow-Origin":      {"https://outside.invalid"},
		"Access-Control-Allow-Credentials": {"true"},
		"Cross-Origin-Resource-Policy":     {"same-origin"},
	}
	if challenge {
		headers["Www-Authenticate"] = []string{`Basic realm="hermes-fixture"`}
	}
	return headers
}

func staticHandler(route, file, contentType string, compressed bool) routeHandler {
	return func(*managementRequest) (response, error) {
		state.hit(route)
		body, err := renderAsset(file)
		if err != nil {
			return response{}, err
		}
		headers := map[string][]string{"Content-Type": {contentType}}
		if compressed {
			// Genuinely gzip-compressed bytes: delivery must decompress them.
			if body, err = gzipBytes(body); err != nil {
				return response{}, err
			}
			headers["Content-Encoding"] = []string{"gzip"}
		}
		return response{StatusCode: 200, Headers: headers, Body: body}, nil
	}
}

func panelHandler(*managementRequest) (response, error) {
	state.hit("/panel")
	body, err := renderAsset("panel.html")
	if err != nil {
		return response{}, err
	}
	headers := hostileHeaders(false)
	headers["Content-Type"] = []string{"text/html; charset=utf-8"}
	return response{StatusCode: 200, Headers: headers, Body: body}, nil
}

var (
	logoOnce  sync.Once
	logoBytes []byte
)

func logoHandler(*managementRequest) (response, error) {
	state.hit("/logo.png")
	logoOnce.Do(func() {
		img := image.NewNRGBA(image.Rect(0, 0, 16, 16))
		for y := range 16 {
			for x := range 16 {
				img.SetNRGBA(x, y, color.NRGBA{R: uint8(x * 16), G: uint8(y * 16), B: 128, A: 255})
			}
		}
		var buffer bytes.Buffer
		if err := png.Encode(&buffer, img); err == nil {
			logoBytes = buffer.Bytes()
		}
	})
	if len(logoBytes) == 0 {
		return response{}, errors.New("fixture image unavailable")
	}
	return response{StatusCode: 200, Headers: map[string][]string{
		"Content-Type": {"image/png"}, "Cache-Control": {"public, max-age=3600"},
	}, Body: logoBytes}, nil
}

func formHandler(req *managementRequest) (response, error) {
	state.hit("/form")
	query := req.Query
	if query == nil {
		query = map[string][]string{}
	}
	payload, err := json.Marshal(query)
	if err != nil {
		return response{}, err
	}
	page := "<!doctype html><html><head><meta charset=\"utf-8\"><title>fixture:form</title></head><body>" +
		"<main id=\"form-result\" data-state=\"complete\" data-plugin-id=\"" + pluginID +
		"\" data-generation=\"" + generation + "\" data-query=\"" + html.EscapeString(string(payload)) + "\">" +
		"<p>GET form received</p><a id=\"back-to-panel\" href=\"panel\">panel</a></main></body></html>"
	return response{StatusCode: 200, Headers: map[string][]string{
		"Content-Type": {"text/html; charset=utf-8"},
	}, Body: []byte(page)}, nil
}

// stateHandler reports counters without counting itself.
func stateHandler(*managementRequest) (response, error) {
	return jsonResponse(200, state.view()), nil
}

func effectHandler(req *managementRequest) (response, error) {
	source := first(req.Query, "source")
	seq := state.effect("/api/effect", "source="+source)
	return jsonResponse(200, map[string]any{
		"ok": true, "effect_count": seq, "generation": generation,
		"instance_nonce": state.nonceValue(), "source": source,
	}), nil
}

func queryHandler(req *managementRequest) (response, error) {
	state.hit("/api/query")
	query := req.Query
	if query == nil {
		query = map[string][]string{}
	}
	total := 0
	for _, items := range query {
		total += len(items)
	}
	return jsonResponse(200, map[string]any{"query": query, "keys": sortedKeys(query), "values": total}), nil
}

// Request headers a presentation host and owner must never forward to a plugin.
var forbiddenRequestHeaders = map[string]bool{
	"authorization": true, "proxy-authorization": true, "cookie": true,
	"x-management-key": true, "x-api-key": true, "forwarded": true,
	"x-forwarded-for": true, "x-forwarded-host": true, "x-real-ip": true,
	"x-cpa-scope": true, "x-cpa-epoch": true,
}

// headersHandler reports header NAMES and a fixed safe subset of values. The
// values of forbidden (secret-bearing) headers are never reflected.
func headersHandler(req *managementRequest) (response, error) {
	state.hit("/api/headers")
	names := make([]string, 0, len(req.Headers))
	forbidden := []string{}
	for name := range req.Headers {
		lower := strings.ToLower(name)
		names = append(names, lower)
		if forbiddenRequestHeaders[lower] || strings.HasPrefix(lower, "x-hermes-") {
			forbidden = append(forbidden, lower)
		}
	}
	sort.Strings(names)
	sort.Strings(forbidden)
	return jsonResponse(200, map[string]any{
		"names": names, "forbidden_present": forbidden,
		"accept": first(req.Headers, "Accept"), "probe": first(req.Headers, "X-Fixture-Probe"),
	}), nil
}

func forgeHandler(req *managementRequest) (response, error) {
	seq := state.effect("/api/forge", "forged-response-headers")
	headers := hostileHeaders(true)
	headers["Content-Type"] = []string{"application/json; charset=utf-8"}
	headers["Content-Disposition"] = []string{`inline; filename="forged.json"`}
	headers["Etag"] = []string{`"fixture-forge"`}
	names := make([]string, 0, len(headers))
	for name := range headers {
		names = append(names, name)
	}
	sort.Strings(names)
	body, err := json.Marshal(map[string]any{"ok": true, "effect_count": seq, "forged_headers": names})
	if err != nil {
		return response{}, err
	}
	return response{StatusCode: 200, Headers: headers, Body: body}, nil
}

func binaryHandler(*managementRequest) (response, error) {
	state.hit("/api/binary")
	body := make([]byte, 1024)
	for index := range body {
		body[index] = byte(index)
	}
	return response{StatusCode: 200, Headers: map[string][]string{
		"Content-Type":        {"application/octet-stream"},
		"Content-Disposition": {`attachment; filename="fixture.bin"`},
		"Content-Language":    {"en"},
		"Etag":                {`"fixture-binary"`},
	}, Body: body}, nil
}

func gzipJSONHandler(*managementRequest) (response, error) {
	state.hit("/api/gzip")
	raw, err := json.Marshal(map[string]any{"gzip": true, "generation": generation})
	if err != nil {
		return response{}, err
	}
	body, err := gzipBytes(raw)
	if err != nil {
		return response{}, err
	}
	return response{StatusCode: 200, Headers: map[string][]string{
		"Content-Type": {"application/json; charset=utf-8"}, "Content-Encoding": {"gzip"},
	}, Body: body}, nil
}

// largeHandler is an effect: even when its response cannot be returned the
// handler really ran, and the owner must say so.
func largeHandler(req *managementRequest) (response, error) {
	size := intParam(req, "bytes", 65536, 0, maxLargeBytes)
	state.effect("/api/large", fmt.Sprintf("bytes=%d", size))
	body := make([]byte, size)
	for index := range body {
		body[index] = byte(index % 251)
	}
	return response{StatusCode: 200, Headers: map[string][]string{
		"Content-Type": {"application/octet-stream"},
	}, Body: body}, nil
}

func failHandler(*managementRequest) (response, error) {
	seq := state.effect("/api/fail", "http-500-after-effect")
	return jsonResponse(500, map[string]any{"ok": false, "error": "fixture_failure_after_effect", "effect_count": seq}), nil
}

func missingHandler(*managementRequest) (response, error) {
	seq := state.effect("/api/missing", "plugin-404-after-effect")
	return jsonResponse(404, map[string]any{"ok": false, "error": "fixture_plugin_404", "effect_count": seq}), nil
}

func errorHandler(*managementRequest) (response, error) {
	state.effect("/api/error", "plugin-error-after-effect")
	return response{}, errors.New("fixture plugin error after effect")
}

// slowHandler counts its effect immediately and answers only after the owner's
// resource deadline, so the owner must report an unknown outcome with the
// effect invoked while the handler still completes exactly once.
func slowHandler(req *managementRequest) (response, error) {
	delay := intParam(req, "ms", 3500, 0, 20000)
	seq := state.effect("/api/slow", fmt.Sprintf("ms=%d", delay))
	time.Sleep(time.Duration(delay) * time.Millisecond)
	state.finished("/api/slow", seq)
	return jsonResponse(200, map[string]any{"ok": true, "effect_count": seq, "slept_ms": delay}), nil
}

func redirectHandler(route, location string, counted bool) routeHandler {
	return func(*managementRequest) (response, error) {
		if counted {
			state.effect(route, "redirect-after-effect")
		} else {
			state.hit(route)
		}
		return response{StatusCode: 302, Headers: map[string][]string{
			"Location": {location}, "Content-Type": {"text/plain; charset=utf-8"},
		}, Body: []byte("redirect")}, nil
	}
}

func redirectTargetHandler(req *managementRequest) (response, error) {
	state.hit("/redirect/target")
	query := req.Query
	if query == nil {
		query = map[string][]string{}
	}
	return jsonResponse(200, map[string]any{"target": true, "query": query, "generation": generation}), nil
}
