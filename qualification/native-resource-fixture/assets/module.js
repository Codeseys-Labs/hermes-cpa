import { staticValue } from './static-dep.js';

document.documentElement.setAttribute('data-module', 'loaded');
document.documentElement.setAttribute('data-module-static', staticValue);

import('__BASE__/dynamic.js').then(function (module) {
  document.documentElement.setAttribute('data-module-dynamic', module.dynamicValue);
}, function () {
  document.documentElement.setAttribute('data-module-dynamic', 'error');
});

import('./gzip-module.js').then(function (module) {
  document.documentElement.setAttribute('data-gzip-module', module.gzipModuleValue);
}, function () {
  document.documentElement.setAttribute('data-gzip-module', 'error');
});
