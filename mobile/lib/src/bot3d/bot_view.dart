import 'package:flutter/material.dart';

import '../api.dart';
import '../appearance.dart';
import 'flat_face.dart';
import 'host.dart';
import 'snapshots.dart';

/// A live, animated 3D bot — the web app's own model, in its studio, doing what its
/// [mood] says. Costs a web view, so it is for the one or two bots on screen that
/// matter (the conversation's header and its welcome); lists use [BotAvatar].
///
/// The flat face shows until the 3D page has loaded, and stays where WebGL is
/// unavailable.
class Bot3D extends StatefulWidget {
  const Bot3D({
    super.key,
    required this.botId,
    required this.appearance,
    required this.mood,
    required this.size,
    this.framing = 'full',
  });

  final String botId;
  final Json appearance;
  final String mood;
  final double size;

  /// `head` crops in on the face, for small avatars.
  final String framing;

  @override
  State<Bot3D> createState() => _Bot3DState();
}

class _Bot3DState extends State<Bot3D> {
  BotHost? _host;
  bool _ready = false;
  bool _webgl = true;

  @override
  void initState() {
    super.initState();
    if (!BotSnapshots.instance.enabled) return;
    _host = BotHost(
      onMessage: (m) {
        if (m['type'] != 'ready' || !mounted) return;
        setState(() {
          _ready = true;
          _webgl = m['webgl'] == true;
        });
        _show();
      },
    );
  }

  void _show() => _host?.call('show', {
    'appearance': widget.appearance,
    'seed': widget.botId,
    'mood': widget.mood,
    'framing': widget.framing,
    'detail': widget.size >= 96 ? 'high' : 'low',
  });

  @override
  void didUpdateWidget(Bot3D old) {
    super.didUpdateWidget(old);
    if (_ready &&
        (old.mood != widget.mood ||
            old.botId != widget.botId ||
            old.framing != widget.framing ||
            old.appearance.toString() != widget.appearance.toString())) {
      _show();
    }
  }

  @override
  void dispose() {
    _host?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final look = appearanceFor(widget.botId, widget.appearance);
    return SizedBox.square(
      dimension: widget.size,
      child: Stack(
        fit: StackFit.expand,
        children: [
          AnimatedOpacity(
            opacity: _ready && _webgl ? 0 : 1,
            duration: const Duration(milliseconds: 250),
            child: FlatFace(look: look, size: widget.size),
          ),
          // The page is transparent until the bot is drawn, so it can sit on top from
          // the start: a platform view that is hidden never loads on the web.
          if (_webgl && _host != null) _host!.view(),
        ],
      ),
    );
  }
}
