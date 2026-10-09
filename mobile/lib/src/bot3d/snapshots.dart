import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../api.dart';
import '../appearance.dart';
import 'disk_cache.dart';
import 'flat_face.dart';
import 'host.dart';

/// Pictures of the 3D bots, for lists.
///
/// A web view per row would be too heavy (and browsers cap live WebGL contexts), so
/// one hidden renderer — [SnapshotRenderer], mounted once under the whole app — draws
/// each bot in its mood with the web app's model and hands back a PNG. Pictures are
/// cached in memory and on disk, keyed by look and mood, so each is drawn once.
class BotSnapshots {
  BotSnapshots._();
  static final instance = BotSnapshots._();

  static const size = 192;
  static const _timeout = Duration(seconds: 12);

  final _memory = <String, Uint8List>{};
  final _waiting = <String, Completer<Uint8List?>>{};
  final _queue = <({String key, Json request})>[];
  final changes = ValueNotifier<int>(0);

  /// Off: every bot is drawn as its flat face and no web view is created. Tests turn
  /// it off; so could a device that cannot run WebGL.
  bool enabled = true;

  BotHost? _host;
  bool _ready = false;
  bool _webgl = true;

  static String keyFor(String botId, Json appearance, String mood) {
    final look = appearanceFor(botId, appearance);
    final parts = [for (final k in look.keys.toList()..sort()) '$k=${look[k]}', mood, 'v1'];
    return base64Url.encode(utf8.encode(parts.join('|'))).replaceAll('=', '');
  }

  Uint8List? cached(String key) => _memory[key];

  /// The picture for this bot in this mood, drawing it if need be; null where 3D is
  /// unavailable or the draw failed (callers show the flat face).
  Future<Uint8List?> get(String botId, Json appearance, String mood) async {
    final key = keyFor(botId, appearance, mood);
    final hit = _memory[key] ?? await _fromDisk(key);
    if (hit != null) return _memory[key] = hit;
    if (!enabled || !_webgl) return null;
    final pending = _waiting[key];
    if (pending != null) return pending.future;
    final completer = _waiting[key] = Completer<Uint8List?>();
    _queue.add((
      key: key,
      request: {
        'id': key,
        'appearance': appearanceFor(botId, appearance),
        'seed': botId,
        'mood': mood,
        'framing': 'head',
        'size': size,
      },
    ));
    _pump();
    return completer.future.timeout(
      _timeout,
      onTimeout: () {
        _waiting.remove(key);
        return null;
      },
    );
  }

  void _attach(BotHost host) => _host = host;

  void _onMessage(Map<String, dynamic> m) {
    switch (m['type']) {
      case 'ready':
        _ready = true;
        _webgl = m['webgl'] == true;
        if (!_webgl) {
          for (final c in _waiting.values) {
            c.complete(null);
          }
          _waiting.clear();
          _queue.clear();
        }
        _pump();
      case 'snapshot':
        final key = m['id'] as String;
        final data = (m['data'] as String).split(',').last;
        final bytes = base64Decode(data);
        _memory[key] = bytes;
        unawaited(_toDisk(key, bytes));
        _waiting.remove(key)?.complete(bytes);
        changes.value++;
      case 'error':
        final id = m['id'];
        if (id is String) _waiting.remove(id)?.complete(null);
    }
  }

  void _pump() {
    final host = _host;
    if (host == null || !_ready) return;
    while (_queue.isNotEmpty) {
      host.call('snapshot', _queue.removeAt(0).request);
    }
  }

  Future<Uint8List?> _fromDisk(String key) => readCached(key);

  Future<void> _toDisk(String key, Uint8List bytes) => writeCached(key, bytes);
}

/// The hidden renderer the snapshots come from. Mount once, behind everything: it has
/// to be laid out (a detached web view does not draw), but nothing ever sees it.
class SnapshotRenderer extends StatefulWidget {
  const SnapshotRenderer({super.key});

  @override
  State<SnapshotRenderer> createState() => _SnapshotRendererState();
}

class _SnapshotRendererState extends State<SnapshotRenderer> {
  BotHost? _host;

  @override
  void initState() {
    super.initState();
    if (!BotSnapshots.instance.enabled) return;
    final host = _host = BotHost(onMessage: BotSnapshots.instance._onMessage);
    BotSnapshots.instance._attach(host);
  }

  @override
  void dispose() {
    _host?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => _host == null
      ? const Positioned(left: 0, top: 0, child: SizedBox.shrink())
      : Positioned(
          left: 0,
          top: 0,
          width: BotSnapshots.size.toDouble(),
          height: BotSnapshots.size.toDouble(),
          child: Opacity(opacity: 0.01, child: _host!.view()),
        );
}

/// A bot's face in a list: its 3D picture in its current mood, the flat face until
/// the picture is ready, and a soft ring while it works.
class BotAvatar extends StatefulWidget {
  const BotAvatar({
    super.key,
    required this.botId,
    required this.appearance,
    this.mood = 'idle',
    this.size = 48,
    this.working = false,
  });

  final String botId;
  final Json appearance;
  final String mood;
  final double size;
  final bool working;

  @override
  State<BotAvatar> createState() => _BotAvatarState();
}

class _BotAvatarState extends State<BotAvatar> with SingleTickerProviderStateMixin {
  Uint8List? _image;
  late final AnimationController _pulse = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1300),
  );

  String get _key => BotSnapshots.keyFor(widget.botId, widget.appearance, widget.mood);

  @override
  void initState() {
    super.initState();
    _load();
    _syncPulse();
    BotSnapshots.instance.changes.addListener(_arrived);
  }

  /// A picture can land after [BotSnapshots.get] gave up waiting (a slow GPU, a long
  /// queue): take it whenever it comes.
  void _arrived() {
    final hit = BotSnapshots.instance.cached(_key);
    if (hit != null && !identical(hit, _image)) setState(() => _image = hit);
  }

  @override
  void didUpdateWidget(BotAvatar old) {
    super.didUpdateWidget(old);
    // A new mood keeps showing the old picture until its own is drawn.
    if (BotSnapshots.keyFor(old.botId, old.appearance, old.mood) != _key) _load();
    _syncPulse();
  }

  void _syncPulse() {
    if (widget.working && !_pulse.isAnimating) {
      _pulse.repeat(reverse: true);
    } else if (!widget.working && _pulse.isAnimating) {
      _pulse
        ..stop()
        ..value = 0;
    }
  }

  void _load() {
    final key = _key;
    final hit = BotSnapshots.instance.cached(key);
    if (hit != null) {
      _image = hit;
      return;
    }
    unawaited(
      BotSnapshots.instance.get(widget.botId, widget.appearance, widget.mood).then((bytes) {
        if (mounted && bytes != null && key == _key) {
          setState(() => _image = bytes);
        }
      }),
    );
  }

  @override
  void dispose() {
    BotSnapshots.instance.changes.removeListener(_arrived);
    _pulse.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final look = appearanceFor(widget.botId, widget.appearance);
    final glow = hexColor(look['glow'], const Color(0xFFF5A623));
    final face = _image == null
        ? FlatFace(look: look, size: widget.size * 0.86)
        : Image.memory(_image!, width: widget.size, height: widget.size, gaplessPlayback: true);
    return SizedBox.square(
      dimension: widget.size,
      child: AnimatedBuilder(
        animation: _pulse,
        builder: (context, child) => DecoratedBox(
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            boxShadow: widget.working
                ? [
                    BoxShadow(
                      color: glow.withValues(alpha: 0.18 + 0.32 * _pulse.value),
                      blurRadius: widget.size * 0.35,
                      spreadRadius: -widget.size * 0.1,
                    ),
                  ]
                : null,
          ),
          child: child,
        ),
        child: Center(
          child: AnimatedSwitcher(duration: const Duration(milliseconds: 250), child: face),
        ),
      ),
    );
  }
}
