/// Where the 3D page runs: a web view on a phone, an iframe in the web build.
/// Both load `assets/bot3d/index.html` and speak the same tiny protocol — see
/// `bot3d/src/embed.tsx`.
library;

export 'host_native.dart' if (dart.library.js_interop) 'host_web.dart';
