import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';

import '../api.dart';
import '../session.dart';
import '../theme.dart';
import 'common.dart';

const _maxBytes = 10 * 1024 * 1024;

/// Where a message is written: one rounded surface floating over the conversation.
/// The text grows with what is typed, photos attach above it, and the round button
/// sends — or, while the bot works and nothing is typed, stops it.
class Composer extends StatefulWidget {
  const Composer({
    super.key,
    required this.controller,
    required this.botId,
    required this.botName,
    required this.working,
    required this.onSend,
    required this.onStop,
  });

  final TextEditingController controller;
  final String botId;
  final String botName;
  final bool working;
  final Future<bool> Function(String text, List<Attachment> attachments) onSend;
  final VoidCallback onStop;

  @override
  State<Composer> createState() => _ComposerState();
}

class _ComposerState extends State<Composer> {
  final _attached = <Attachment>[];
  int _uploading = 0;
  bool _sending = false;

  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_changed);
  }

  @override
  void dispose() {
    widget.controller.removeListener(_changed);
    super.dispose();
  }

  void _changed() => setState(() {});

  bool get _hasText => widget.controller.text.trim().isNotEmpty;
  bool get _canSend => (_hasText || _attached.isNotEmpty) && _uploading == 0 && !_sending;

  Future<void> _send() async {
    if (!_canSend) return;
    tap();
    setState(() => _sending = true);
    final ok = await widget.onSend(widget.controller.text.trim(), List.of(_attached));
    if (!mounted) return;
    setState(() {
      _sending = false;
      if (ok) {
        widget.controller.clear();
        _attached.clear();
      }
    });
  }

  Future<void> _attach(ImageSource source) async {
    final picker = ImagePicker();
    final List<XFile> picked;
    try {
      picked = source == ImageSource.camera
          ? [?await picker.pickImage(source: ImageSource.camera, imageQuality: 85)]
          : await picker.pickMultiImage(imageQuality: 85, limit: 6);
    } catch (e) {
      _toast('Couldn’t open photos: $e');
      return;
    }
    if (picked.isEmpty || !mounted) return;
    final files = <({String name, String mediaType, Uint8List bytes})>[];
    for (final f in picked) {
      final bytes = await f.readAsBytes();
      if (bytes.length > _maxBytes) return _toast('${f.name} is over 10 MB');
      files.add((name: f.name, mediaType: f.mimeType ?? 'image/jpeg', bytes: bytes));
    }
    if (!mounted) return;
    final api = SessionScope.read(context).api;
    setState(() => _uploading += files.length);
    try {
      final stored = await api.uploadFiles(widget.botId, files);
      if (mounted) setState(() => _attached.addAll(stored));
    } on ApiError catch (e) {
      _toast('Couldn’t attach: ${e.message}');
    } finally {
      if (mounted) setState(() => _uploading -= files.length);
    }
  }

  void _toast(String text) {
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
    }
  }

  void _chooseAttach() {
    tap();
    showModalBottomSheet<void>(
      context: context,
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: const Icon(Icons.photo_library_outlined),
              title: const Text('Photo library'),
              onTap: () {
                Navigator.pop(context);
                _attach(ImageSource.gallery);
              },
            ),
            ListTile(
              leading: const Icon(Icons.photo_camera_outlined),
              title: const Text('Take a photo'),
              onTap: () {
                Navigator.pop(context);
                _attach(ImageSource.camera);
              },
            ),
            const SizedBox(height: 8),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final showStop = widget.working && !_hasText && _attached.isEmpty;
    return Container(
      padding: const EdgeInsets.fromLTRB(3, 0, 3, 3),
      decoration: BoxDecoration(
        color: p.raised,
        border: Border.all(color: p.line),
        borderRadius: BorderRadius.circular(28),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (_attached.isNotEmpty || _uploading > 0)
            SizedBox(
              height: 44,
              child: ListView(
                scrollDirection: Axis.horizontal,
                padding: const EdgeInsets.only(left: 9, top: 10, bottom: 2),
                children: [
                  for (final a in _attached)
                    _Chip(
                      icon: Icons.image_outlined,
                      label: a.name,
                      onRemove: () => setState(() => _attached.remove(a)),
                    ),
                  if (_uploading > 0) const _Chip(icon: null, label: 'Uploading…'),
                ],
              ),
            ),
          TextField(
            controller: widget.controller,
            minLines: 1,
            maxLines: 6,
            textCapitalization: TextCapitalization.sentences,
            style: TextStyle(color: p.text, fontSize: 16, height: 1.4),
            decoration: InputDecoration(
              isCollapsed: true,
              border: InputBorder.none,
              // Tall enough to be a 48-point target, laid out as before.
              contentPadding: const EdgeInsets.fromLTRB(15, 14, 15, 6),
              constraints: const BoxConstraints(minHeight: 48),
              hintText: 'Ask ${widget.botName} anything',
              hintMaxLines: 1,
              hintStyle: TextStyle(
                color: p.textFaint,
                fontSize: 16,
                overflow: TextOverflow.ellipsis,
              ),
            ),
          ),
          Row(
            children: [
              _Round(
                tooltip: 'Attach a photo',
                onTap: _chooseAttach,
                border: p.lineStrong,
                child: Icon(Icons.add, color: p.text, size: 22),
              ),
              const Spacer(),
              if (showStop)
                _Round(
                  tooltip: 'Stop ${widget.botName}',
                  onTap: () {
                    tap();
                    widget.onStop();
                  },
                  fill: p.accent,
                  child: Container(
                    width: 12,
                    height: 12,
                    decoration: BoxDecoration(
                      color: p.onAccent,
                      borderRadius: BorderRadius.circular(2),
                    ),
                  ),
                )
              else
                _Round(
                  tooltip: 'Send',
                  onTap: _canSend ? _send : null,
                  fill: _canSend ? p.accent : p.pressed,
                  child: _sending
                      ? SizedBox.square(
                          dimension: 16,
                          child: CircularProgressIndicator(strokeWidth: 2, color: p.onAccent),
                        )
                      : Icon(
                          Icons.arrow_upward,
                          size: 20,
                          color: _canSend ? p.onAccent : p.textFaint,
                        ),
                ),
            ],
          ),
        ],
      ),
    );
  }
}

class _Round extends StatelessWidget {
  const _Round({
    required this.tooltip,
    required this.onTap,
    required this.child,
    this.fill,
    this.border,
  });
  final String tooltip;
  final VoidCallback? onTap;
  final Widget child;
  final Color? fill;
  final Color? border;

  @override
  Widget build(BuildContext context) => Tooltip(
    message: tooltip,
    child: Semantics(
      button: true,
      enabled: onTap != null,
      label: tooltip,
      excludeSemantics: true,
      // A 38-point circle to look at, a 48-point target to hit.
      child: InkResponse(
        onTap: onTap,
        radius: 24,
        child: SizedBox.square(
          dimension: 48,
          child: Center(
            child: Material(
              color: fill ?? Colors.transparent,
              shape: CircleBorder(
                side: border == null ? BorderSide.none : BorderSide(color: border!),
              ),
              child: SizedBox.square(dimension: 38, child: Center(child: child)),
            ),
          ),
        ),
      ),
    ),
  );
}

class _Chip extends StatelessWidget {
  const _Chip({required this.icon, required this.label, this.onRemove});
  final IconData? icon;
  final String label;
  final VoidCallback? onRemove;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Container(
      margin: const EdgeInsets.only(right: 6),
      padding: EdgeInsets.only(left: 10, right: onRemove == null ? 10 : 2),
      constraints: const BoxConstraints(maxWidth: 220),
      decoration: BoxDecoration(color: p.pressed, borderRadius: BorderRadius.circular(14)),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        spacing: 6,
        children: [
          if (icon != null)
            Icon(icon, size: 15, color: p.textDim)
          else
            SizedBox.square(
              dimension: 12,
              child: CircularProgressIndicator(strokeWidth: 1.5, color: p.textDim),
            ),
          Flexible(
            child: Text(
              label,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(color: p.text, fontSize: 13),
            ),
          ),
          if (onRemove != null)
            Semantics(
              button: true,
              label: 'Remove $label',
              child: InkResponse(
                onTap: onRemove,
                radius: 16,
                child: Padding(
                  padding: const EdgeInsets.all(6),
                  child: Icon(Icons.close, size: 15, color: p.textDim),
                ),
              ),
            ),
        ],
      ),
    );
  }
}
