(function () {
  'use strict';

  // Deterministic browser-visible markers. Functional checks end in pass/fail.
  // Containment checks end in contained/LEAK/inconclusive. #fixture[data-state]
  // becomes "complete" once every automatic check has run; data-failures counts
  // "fail" results and data-leaks counts "LEAK" results. Nothing here navigates
  // or spends an effect on its own: navigation and the counted effect are
  // explicit button clicks.
  var script = document.currentScript;
  var scriptUrl = script && script.src ? script.src : '';
  var root = document.getElementById('fixture');
  var base = root.getAttribute('data-base');
  var generation = root.getAttribute('data-generation');
  var list = document.getElementById('checks');
  var finished = false;
  var report = {
    plugin_id: root.getAttribute('data-plugin-id'),
    generation: generation,
    location: { href: location.href, origin: location.origin, protocol: location.protocol, pathname: location.pathname },
    checks: {},
    violations: [],
    native_state: null
  };
  var violations = report.violations;

  document.addEventListener('securitypolicyviolation', function (event) {
    violations.push({ directive: event.violatedDirective, blocked: event.blockedURI });
  });

  function mark(name, result, detail) {
    if (finished) { return; }
    report.checks[name] = { result: result, detail: detail === undefined ? null : detail };
    root.setAttribute('data-check-' + name, result);
    var item = document.createElement('li');
    item.id = 'check-' + name;
    item.setAttribute('data-result', result);
    item.textContent = name + ': ' + result;
    list.appendChild(item);
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function waitFor(test, timeout) {
    return new Promise(function (resolve) {
      var started = Date.now();
      (function poll() {
        var value = test();
        if (value || Date.now() - started > timeout) { resolve(value); return; }
        setTimeout(poll, 25);
      })();
    });
  }

  function getJson(url, options) {
    return fetch(url, options || { cache: 'no-store' }).then(function (response) {
      return response.json().then(function (value) {
        return { status: response.status, ok: response.ok, url: response.url, redirected: response.redirected, value: value };
      });
    });
  }

  function attribute(name) {
    return document.documentElement.getAttribute(name);
  }

  function cspSince(count, pattern) {
    return violations.slice(count).some(function (entry) { return pattern.test(entry.directive); });
  }

  function guarded(name, task) {
    return function () {
      return Promise.resolve().then(task).catch(function (error) {
        mark(name, 'fail', { error: String(error) });
      });
    };
  }

  function attributeCheck(name, attributeName, expected) {
    return guarded(name, function () {
      return waitFor(function () { return attribute(attributeName) !== null; }, 5000).then(function () {
        var value = attribute(attributeName);
        mark(name, value === expected ? 'pass' : 'fail', { value: value });
      });
    });
  }

  function redirectIn(name, path, hop) {
    return guarded(name, function () {
      return getJson(path).then(function (r) {
        var finalPath = new URL(r.url).pathname;
        var good = r.ok && r.value && r.value.target === true && finalPath === base + '/redirect/target' &&
          JSON.stringify(r.value.query.hop) === JSON.stringify([hop]);
        mark(name, good ? 'pass' : 'fail', { url: r.url, status: r.status });
      });
    });
  }

  function redirectOut(name, path) {
    return guarded(name, function () {
      return fetch(path, { cache: 'no-store' }).then(function (response) {
        var host = '';
        try { host = new URL(response.url).hostname; } catch (ignored) { host = ''; }
        mark(name, host === 'outside.invalid' ? 'LEAK' : (response.ok ? 'inconclusive' : 'contained'),
          { status: response.status, redirected: response.redirected });
      }, function () {
        mark(name, 'contained', { rejected: true });
      });
    });
  }

  function fallthrough(name, path) {
    return guarded(name, function () {
      return fetch(path, { cache: 'no-store' }).then(function (response) {
        mark(name, response.ok ? 'LEAK' : 'contained', { status: response.status });
      }, function () {
        mark(name, 'contained', { rejected: true });
      });
    });
  }

  // contained requires an actual CSP violation event: a bare network failure
  // cannot tell policy from a missing network.
  function outsideLoad(name, directive, begin) {
    return guarded(name, function () {
      var before = violations.length;
      return new Promise(function (resolve) {
        begin(function () { resolve('loaded'); }, function () { resolve('blocked'); });
      }).then(function (outcome) {
        if (outcome === 'loaded') { mark(name, 'LEAK', null); return undefined; }
        return sleep(150).then(function () {
          var event = cspSince(before, directive);
          mark(name, event ? 'contained' : 'inconclusive', { csp_event: event });
        });
      });
    });
  }

  var tasks = [
    guarded('entry', function () {
      mark('entry', location.pathname === base + '/panel' ? 'pass' : 'fail',
        { pathname: location.pathname, protocol: location.protocol, origin: location.origin });
    }),
    guarded('script-relative', function () {
      mark('script-relative', scriptUrl === new URL('app.js', location.href).href ? 'pass' : 'fail', { src: scriptUrl });
    }),
    guarded('css-relative', function () {
      var color = getComputedStyle(document.getElementById('probe-css')).color;
      mark('css-relative', color === 'rgb(10, 20, 30)' ? 'pass' : 'fail', { color: color });
    }),
    guarded('image-relative', function () {
      var image = document.getElementById('probe-img');
      return waitFor(function () { return image.complete; }, 5000).then(function () {
        mark('image-relative', image.complete && image.naturalWidth === 16 && image.naturalHeight === 16 ? 'pass' : 'fail',
          { width: image.naturalWidth, height: image.naturalHeight });
      });
    }),
    attributeCheck('module-root-relative', 'data-module-static', 'static-ok-g' + generation),
    attributeCheck('module-dynamic', 'data-module-dynamic', 'dynamic-ok-g' + generation),
    attributeCheck('gzip-script', 'data-gzip-script', 'gzip-script-ok-g' + generation),
    attributeCheck('gzip-module', 'data-gzip-module', 'gzip-module-ok-g' + generation),
    guarded('fetch-json', function () {
      return getJson('api/state').then(function (r) {
        report.native_state = r.value;
        mark('fetch-json', r.ok && r.value && r.value.plugin_id === report.plugin_id && r.value.generation === generation ? 'pass' : 'fail',
          { status: r.status });
      });
    }),
    guarded('fetch-query', function () {
      var expected = { a: ['1', '2'], b: ['%'], c: ['x y'], d: ['\u00e9'], e: ['a&b'] };
      return getJson('api/query?a=1&a=2&b=%25&c=x+y&d=%C3%A9&e=a%26b').then(function (r) {
        mark('fetch-query', r.ok && JSON.stringify(r.value.query) === JSON.stringify(expected) ? 'pass' : 'fail', { query: r.value.query });
      });
    }),
    guarded('fetch-binary', function () {
      return fetch('api/binary', { cache: 'no-store' }).then(function (response) {
        return response.arrayBuffer();
      }).then(function (buffer) {
        var bytes = new Uint8Array(buffer);
        var good = bytes.length === 1024;
        for (var index = 0; good && index < bytes.length; index += 1) {
          if (bytes[index] !== (index & 255)) { good = false; }
        }
        mark('fetch-binary', good ? 'pass' : 'fail', { length: bytes.length });
      });
    }),
    guarded('fetch-gzip', function () {
      return getJson('api/gzip').then(function (r) {
        mark('fetch-gzip', r.ok && r.value.gzip === true && r.value.generation === generation ? 'pass' : 'fail', { status: r.status });
      });
    }),
    guarded('headers-sanitized', function () {
      var options = {
        cache: 'no-store', credentials: 'include',
        headers: {
          'X-Fixture-Probe': 'browser',
          'X-Hermes-Resource-Invoked': '0',
          'X-Hermes-Egress-Policy': 'forged',
          'X-Hermes-Config-Revision': 'forged',
          'Authorization': 'Bearer hermes-fixture-forged-not-a-secret'
        }
      };
      return getJson('api/headers', options).then(function (r) {
        var leaked = r.value.forbidden_present;
        mark('headers-sanitized', r.ok && leaked.length === 0 ? 'contained' : 'LEAK',
          { forbidden_present: leaked, probe: r.value.probe });
      }, function () {
        mark('headers-sanitized', 'contained', { preflight: 'blocked' });
      });
    }),
    redirectIn('redirect-relative', 'redirect/start', '1'),
    redirectIn('redirect-absolute-path', 'redirect/absolute', '2'),
    redirectIn('redirect-dot-segments', 'redirect/dotdot', '3'),
    redirectOut('redirect-outside', 'redirect/outside'),
    redirectOut('redirect-protocol-relative', 'redirect/protocol-relative'),
    redirectOut('redirect-unregistered', 'redirect/unregistered'),
    outsideLoad('outside-fetch', /connect-src|default-src/, function (loaded, blocked) {
      fetch('https://outside.invalid/hermes-fixture-probe', { mode: 'no-cors', cache: 'no-store' }).then(loaded, blocked);
    }),
    outsideLoad('outside-image', /img-src|default-src/, function (loaded, blocked) {
      var image = new Image();
      image.onload = loaded;
      image.onerror = blocked;
      image.src = 'https://outside.invalid/hermes-fixture-image.png';
    }),
    outsideLoad('outside-script', /script-src|default-src/, function (loaded, blocked) {
      var element = document.createElement('script');
      element.onload = loaded;
      element.onerror = blocked;
      element.src = 'https://outside.invalid/hermes-fixture-script.js';
      document.head.appendChild(element);
    }),
    fallthrough('root-fallthrough', '/'),
    fallthrough('api-fallthrough', '/api/status'),
    fallthrough('plugin-api-fallthrough', '/api/plugins/hermes-cpa/status'),
    fallthrough('management-fallthrough', '/v8/management/config'),
    fallthrough('unregistered-resource', base + '/not-registered'),
    guarded('frame-isolation', function () {
      if (window.top === window) { mark('frame-isolation', 'inconclusive', { framed: false }); return; }
      var result = 'contained';
      try { void window.parent.document.title; result = 'LEAK'; } catch (ignored) { result = 'contained'; }
      mark('frame-isolation', result, { framed: true });
    }),
    guarded('storage-isolation', function () {
      if (window.top === window) { mark('storage-isolation', 'inconclusive', { framed: false }); return; }
      var storage = 'contained';
      var cookie = 'contained';
      try { void window.localStorage.length; storage = 'LEAK'; } catch (ignored) { storage = 'contained'; }
      try {
        document.cookie = 'hermes_fixture=1';
        if (document.cookie.indexOf('hermes_fixture') >= 0) { cookie = 'LEAK'; }
      } catch (ignored) { cookie = 'contained'; }
      mark('storage-isolation', storage === 'contained' && cookie === 'contained' ? 'contained' : 'LEAK',
        { storage: storage, cookie: cookie });
    }),
    guarded('service-worker', function () {
      var worker;
      try { worker = navigator.serviceWorker; } catch (ignored) { worker = undefined; }
      if (!worker) { mark('service-worker', 'contained', { available: false }); return undefined; }
      return worker.register('sw.js').then(function () {
        mark('service-worker', 'LEAK', null);
      }, function () {
        mark('service-worker', 'contained', { rejected: true });
      });
    })
  ];

  function finish() {
    if (finished) { return; }
    var failures = 0;
    var leaks = 0;
    Object.keys(report.checks).forEach(function (name) {
      var result = report.checks[name].result;
      if (result === 'fail') { failures += 1; }
      if (result === 'LEAK') { leaks += 1; }
    });
    report.failures = failures;
    report.leaks = leaks;
    document.getElementById('fixture-report').textContent = JSON.stringify(report);
    root.setAttribute('data-failures', String(failures));
    root.setAttribute('data-leaks', String(leaks));
    root.setAttribute('data-state', 'complete');
    document.documentElement.setAttribute('data-fixture-state', 'complete');
    document.title = 'fixture:complete:' + failures + ':' + leaks;
    finished = true;
  }

  // The counted effect: one click is exactly one native GET effect.
  document.getElementById('effect-button').addEventListener('click', function () {
    root.setAttribute('data-effect-state', 'running');
    getJson('api/effect?source=button').then(function (r) {
      root.setAttribute('data-effect-status', String(r.status));
      root.setAttribute('data-effect-count', r.ok && r.value ? String(r.value.effect_count) : 'error');
      document.getElementById('effect-result').textContent = JSON.stringify(r.value);
      root.setAttribute('data-effect-state', 'done');
    }, function () {
      root.setAttribute('data-effect-count', 'error');
      root.setAttribute('data-effect-state', 'done');
    });
  });

  // Each outside-navigation button records "attempted" first. If this page is
  // still alive 750 ms later the attempt stayed contained ("stayed"); a page
  // that navigated away leaves no marker and the frame URL is the evidence.
  function navigationAttempt(buttonId, name, action) {
    document.getElementById(buttonId).addEventListener('click', function () {
      root.setAttribute('data-nav-' + name, 'attempted');
      try { action(); } catch (ignored) { root.setAttribute('data-nav-' + name, 'blocked-exception'); }
      setTimeout(function () {
        if (root.getAttribute('data-nav-' + name) === 'attempted') { root.setAttribute('data-nav-' + name, 'stayed'); }
      }, 750);
    });
  }
  navigationAttempt('nav-location-outside', 'location', function () {
    location.href = 'https://outside.invalid/hermes-fixture-navigation';
  });
  navigationAttempt('nav-open-outside', 'open', function () {
    var opened = window.open('https://outside.invalid/hermes-fixture-popup', '_blank');
    if (opened) { opened.close(); root.setAttribute('data-nav-open', 'LEAK'); }
  });
  navigationAttempt('nav-link-outside', 'link', function () {
    document.getElementById('outside-link-self').click();
  });
  navigationAttempt('nav-link-blank-outside', 'link-blank', function () {
    document.getElementById('outside-link-blank').click();
  });
  navigationAttempt('nav-form-outside', 'form', function () {
    document.getElementById('outside-form').submit();
  });
  navigationAttempt('nav-meta-outside', 'meta', function () {
    var meta = document.createElement('meta');
    meta.httpEquiv = 'refresh';
    meta.content = '0;url=https://outside.invalid/hermes-fixture-meta';
    document.head.appendChild(meta);
  });

  setTimeout(function () {
    if (!finished) { mark('watchdog', 'fail', { seconds: 90 }); finish(); }
  }, 90000);

  tasks.reduce(function (previous, task) { return previous.then(task); }, Promise.resolve()).then(finish, finish);
})();
