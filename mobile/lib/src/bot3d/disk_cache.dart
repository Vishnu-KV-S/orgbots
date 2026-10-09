/// Snapshot pictures on disk, so each bot is drawn once per look and mood, not once
/// per launch. A no-op in the web build, where the browser caches nothing for us.
library;

export 'disk_cache_io.dart' if (dart.library.js_interop) 'disk_cache_web.dart';
