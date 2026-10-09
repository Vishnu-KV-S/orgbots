import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:webview_flutter/webview_flutter.dart';

import '../api.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets/common.dart';

/// The bot's own screen, live. Watching is free; "Take control" pauses the bot and
/// hands you its browser — tap to click, type into the field below, scroll, or open an
/// address — for the moments only a person can handle: a sign-in, a CAPTCHA, a
/// confirmation. Leaving gives control back.
///
/// The picture is the server's MJPEG stream in a web view whose base URL is the
/// server, so it carries the session cookie; taps come back from the page as fractions
/// of the picture and become clicks in the bot's 1280×800 viewport.
class LiveScreen extends StatefulWidget {
  const LiveScreen({super.key, required this.botId, required this.botName});
  final String botId;
  final String botName;

  @override
  State<LiveScreen> createState() => _LiveScreenState();
}

class _LiveScreenState extends State<LiveScreen> {
  WebViewController? _web;
  bool _human = false;
  bool _busy = false;
  String? _error;
  String _url = '';
  final _text = TextEditingController();
  final _address = TextEditingController();
  late final Api _api = SessionScope.read(context).api;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (kIsWeb || _web != null) return;
    final session = SessionScope.read(context);
    final p = Palette.of(context);
    String hex(Color c) => '#${(c.toARGB32() & 0xFFFFFF).toRadixString(16).padLeft(6, '0')}';
    final html =
        '''<!doctype html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=4">
<style>
  html,body{margin:0;height:100%;background:${hex(p.bg)};display:flex;align-items:center;justify-content:center}
  img{width:100%;height:auto;display:block;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
  #off{color:${hex(p.textFaint)};font:14px -apple-system,system-ui,sans-serif;text-align:center;padding:24px;display:none}
</style></head><body>
<img id="s" src="${_api.streamUrl(widget.botId)}" alt="">
<div id="off">The screen isn’t available right now.</div>
<script>
  var s=document.getElementById('s');
  s.onerror=function(){s.style.display='none';document.getElementById('off').style.display='block'};
  s.addEventListener('click',function(e){
    var r=s.getBoundingClientRect();
    Tap.postMessage(JSON.stringify({fx:(e.clientX-r.left)/r.width,fy:(e.clientY-r.top)/r.height}));
  });
</script></body></html>''';
    _web = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(p.bg)
      ..addJavaScriptChannel('Tap', onMessageReceived: (m) => _tapped(m.message));
    unawaited(
      session.shareSessionWithWebViews().then(
        (_) => _web!.loadHtmlString(html, baseUrl: session.server),
      ),
    );
  }

  @override
  void dispose() {
    // Give the screen back however this view closes.
    if (_human) {
      unawaited(_api.setController(widget.botId, 'bot').catchError((_) {}));
    }
    _text.dispose();
    _address.dispose();
    super.dispose();
  }

  Future<void> _send(List<Json> events) async {
    try {
      final url = await _api.sendInputs(widget.botId, events);
      if (mounted) {
        setState(() {
          if (url.isNotEmpty) _url = url;
          _error = null;
        });
      }
    } on ApiError catch (e) {
      if (mounted) setState(() => _error = e.message);
    }
  }

  void _tapped(String message) {
    if (!_human) return;
    final Json data;
    try {
      data = jsonDecode(message) as Json;
    } catch (_) {
      return;
    }
    HapticFeedback.selectionClick();
    final x = ((data['fx'] as num).clamp(0, 1) * (viewportWidth - 1)).round();
    final y = ((data['fy'] as num).clamp(0, 1) * (viewportHeight - 1)).round();
    _send([
      {'kind': 'move', 'x': x, 'y': y},
      {'kind': 'down', 'x': x, 'y': y, 'button': 'left', 'clicks': 1},
      {'kind': 'up', 'x': x, 'y': y, 'button': 'left', 'clicks': 1},
    ]);
  }

  Future<void> _toggle() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final next = _human ? 'bot' : 'human';
      await _api.setController(widget.botId, next);
      setState(() => _human = next == 'human');
    } on ApiError catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _scroll(int dy) => _send([
    {'kind': 'wheel', 'x': viewportWidth ~/ 2, 'y': viewportHeight ~/ 2, 'dx': 0, 'dy': dy},
  ]);

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    const amber = Color(0xFFFFB02E);
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.close),
          tooltip: 'Close',
          onPressed: () => Navigator.pop(context),
        ),
        title: Column(
          children: [
            Text(
              '${widget.botName}’s screen',
              style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w700),
            ),
            Row(
              mainAxisSize: MainAxisSize.min,
              spacing: 6,
              children: [
                Container(
                  width: 7,
                  height: 7,
                  decoration: BoxDecoration(
                    color: _human ? amber : const Color(0xFF3DFFB0),
                    shape: BoxShape.circle,
                  ),
                ),
                Text(
                  _human ? 'You have control — the bot is paused' : 'Live',
                  style: TextStyle(color: p.textFaint, fontSize: 12),
                ),
              ],
            ),
          ],
        ),
      ),
      body: SafeArea(
        top: false,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Container(
              margin: const EdgeInsets.fromLTRB(12, 8, 12, 0),
              decoration: BoxDecoration(
                border: Border.all(color: _human ? amber : p.line, width: 1.5),
                borderRadius: BorderRadius.circular(Radii.md),
              ),
              clipBehavior: Clip.antiAlias,
              child: AspectRatio(
                aspectRatio: viewportWidth / viewportHeight,
                child: _web == null
                    ? Center(
                        child: Text(
                          'The live screen is in the phone app.',
                          style: TextStyle(color: p.textFaint),
                        ),
                      )
                    : WebViewWidget(controller: _web!),
              ),
            ),
            if (_url.isNotEmpty)
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 6, 16, 0),
                child: Text(
                  _url,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(color: p.textFaint, fontSize: 11.5),
                ),
              ),
            const Spacer(),
            Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                spacing: 10,
                children: [
                  if (_error != null) Notice(_error!),
                  if (_human) ...[
                    Text(
                      'Tap the picture to click. Pinch to zoom.',
                      textAlign: TextAlign.center,
                      style: TextStyle(color: p.textDim, fontSize: 13.5),
                    ),
                    Row(
                      spacing: 8,
                      children: [
                        _Tool(
                          Icons.arrow_back,
                          'Back',
                          () => _send([
                            {'kind': 'back'},
                          ]),
                        ),
                        _Tool(
                          Icons.refresh,
                          'Reload',
                          () => _send([
                            {'kind': 'reload'},
                          ]),
                        ),
                        _Tool(Icons.keyboard_arrow_up, 'Scroll up', () => _scroll(-500)),
                        _Tool(Icons.keyboard_arrow_down, 'Scroll down', () => _scroll(500)),
                        _Tool(
                          Icons.keyboard_return,
                          'Enter',
                          () => _send([
                            {'kind': 'key', 'key': 'Enter'},
                          ]),
                        ),
                      ],
                    ),
                    _Field(
                      controller: _text,
                      hint: 'Type into the focused field',
                      icon: Icons.arrow_upward,
                      onSubmit: (v) {
                        if (v.isEmpty) return;
                        _send([
                          {'kind': 'type', 'text': v},
                        ]);
                        _text.clear();
                      },
                    ),
                    _Field(
                      controller: _address,
                      hint: 'Go to address',
                      icon: Icons.public,
                      onSubmit: (v) {
                        final raw = v.trim();
                        if (raw.isEmpty) return;
                        _send([
                          {
                            'kind': 'navigate',
                            'url': RegExp('^https?://').hasMatch(raw) ? raw : 'https://$raw',
                          },
                        ]);
                        _address.clear();
                      },
                    ),
                    PillButton(label: 'Give control back', busy: _busy, onPressed: _toggle),
                  ] else ...[
                    Text(
                      'Take control to sign in, solve a CAPTCHA or finish a step yourself. The bot pauses until you hand it back.',
                      textAlign: TextAlign.center,
                      style: TextStyle(color: p.textDim, fontSize: 13.5, height: 1.45),
                    ),
                    PillButton(
                      label: 'Take control',
                      icon: Icons.back_hand_outlined,
                      busy: _busy,
                      onPressed: _toggle,
                    ),
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Tool extends StatelessWidget {
  const _Tool(this.icon, this.label, this.onTap);
  final IconData icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Expanded(
      child: Tooltip(
        message: label,
        child: Material(
          color: p.raised,
          borderRadius: BorderRadius.circular(Radii.md),
          child: InkWell(
            borderRadius: BorderRadius.circular(Radii.md),
            onTap: () {
              tap();
              onTap();
            },
            child: SizedBox(height: 44, child: Icon(icon, size: 20, color: p.text)),
          ),
        ),
      ),
    );
  }
}

class _Field extends StatelessWidget {
  const _Field({
    required this.controller,
    required this.hint,
    required this.icon,
    required this.onSubmit,
  });
  final TextEditingController controller;
  final String hint;
  final IconData icon;
  final void Function(String) onSubmit;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return TextField(
      controller: controller,
      autocorrect: false,
      textInputAction: TextInputAction.send,
      onSubmitted: onSubmit,
      style: TextStyle(color: p.text, fontSize: 16),
      decoration: fieldDecoration(context, hint: hint, pill: true).copyWith(
        suffixIcon: Padding(
          padding: const EdgeInsets.all(5),
          child: Material(
            color: p.accent,
            shape: const CircleBorder(),
            child: InkWell(
              customBorder: const CircleBorder(),
              onTap: () => onSubmit(controller.text),
              child: SizedBox.square(dimension: 38, child: Icon(icon, size: 18, color: p.onAccent)),
            ),
          ),
        ),
      ),
    );
  }
}
