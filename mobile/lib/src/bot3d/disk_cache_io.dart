import 'dart:io';
import 'dart:typed_data';

import 'package:path_provider/path_provider.dart';

Future<String?>? _dir;

Future<String?> _cacheDir() => _dir ??= () async {
  try {
    final dir = Directory('${(await getApplicationCacheDirectory()).path}/bot3d');
    await dir.create(recursive: true);
    return dir.path;
  } catch (_) {
    return null;
  }
}();

Future<Uint8List?> readCached(String key) async {
  final dir = await _cacheDir();
  if (dir == null) return null;
  try {
    final file = File('$dir/$key.png');
    return await file.exists() ? await file.readAsBytes() : null;
  } catch (_) {
    return null;
  }
}

Future<void> writeCached(String key, Uint8List bytes) async {
  final dir = await _cacheDir();
  if (dir == null) return;
  try {
    await File('$dir/$key.png').writeAsBytes(bytes, flush: true);
  } catch (_) {}
}
