import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../theme.dart';

final _inline = RegExp(r'''(https?://[^\s<>()]+[^\s<>().,;:!?'"]|`[^`\n]+`|\*\*[^*\n]+\*\*)''');
final _bullet = RegExp(r'^\s*([-*•]|\d+\.)\s+(.*)$');
final _heading = RegExp(r'^#{1,4}\s+(.*)$');

/// A bot's reply, rendered as text. Replies carry what bots read off web pages, so they
/// are untrusted: the few things a reply uses — links, `code`, **bold**, bullet and
/// numbered lines, headings — become styled text and everything else stays text.
class BotText extends StatefulWidget {
  const BotText(this.text, {super.key});
  final String text;

  @override
  State<BotText> createState() => _BotTextState();
}

class _BotTextState extends State<BotText> {
  final _recognizers = <TapGestureRecognizer>[];

  @override
  void dispose() {
    for (final r in _recognizers) {
      r.dispose();
    }
    super.dispose();
  }

  List<InlineSpan> _spans(String line, Palette p) {
    final out = <InlineSpan>[];
    var last = 0;
    for (final match in _inline.allMatches(line)) {
      if (match.start > last) {
        out.add(TextSpan(text: line.substring(last, match.start)));
      }
      final token = match[0]!;
      if (token.startsWith('`')) {
        out.add(
          TextSpan(
            text: token.substring(1, token.length - 1),
            style: TextStyle(fontFamily: 'monospace', fontSize: 14, backgroundColor: p.raised),
          ),
        );
      } else if (token.startsWith('**')) {
        out.add(
          TextSpan(
            text: token.substring(2, token.length - 2),
            style: const TextStyle(fontWeight: FontWeight.w700),
          ),
        );
      } else {
        final recognizer = TapGestureRecognizer()
          ..onTap = () => launchUrl(Uri.parse(token), mode: LaunchMode.externalApplication);
        _recognizers.add(recognizer);
        out.add(
          TextSpan(
            text: token,
            recognizer: recognizer,
            style: const TextStyle(decoration: TextDecoration.underline),
          ),
        );
      }
      last = match.end;
    }
    if (last < line.length) out.add(TextSpan(text: line.substring(last)));
    return out;
  }

  @override
  Widget build(BuildContext context) {
    for (final r in _recognizers) {
      r.dispose();
    }
    _recognizers.clear();
    final p = Palette.of(context);
    final base = TextStyle(color: p.text, fontSize: 16, height: 1.5);
    final blocks = <Widget>[];
    final paragraph = <String>[];

    void flush() {
      if (paragraph.isEmpty) return;
      final lines = List.of(paragraph);
      paragraph.clear();
      blocks.add(
        Text.rich(
          TextSpan(
            style: base,
            children: [
              for (var i = 0; i < lines.length; i++) ...[
                ..._spans(lines[i], p),
                if (i < lines.length - 1) const TextSpan(text: '\n'),
              ],
            ],
          ),
        ),
      );
    }

    for (final line in widget.text.split('\n')) {
      final bullet = _bullet.firstMatch(line);
      final heading = _heading.firstMatch(line);
      if (bullet != null) {
        flush();
        final marker = RegExp(r'\d').hasMatch(bullet[1]!) ? bullet[1]! : '•';
        blocks.add(
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              SizedBox(width: 22, child: Text(marker, style: base)),
              Expanded(
                child: Text.rich(TextSpan(style: base, children: _spans(bullet[2]!, p))),
              ),
            ],
          ),
        );
      } else if (heading != null) {
        flush();
        blocks.add(
          Text.rich(
            TextSpan(
              style: base.copyWith(fontWeight: FontWeight.w700, fontSize: 17),
              children: _spans(heading[1]!, p),
            ),
          ),
        );
      } else if (line.trim().isEmpty) {
        flush();
      } else {
        paragraph.add(line);
      }
    }
    flush();
    return SelectionArea(
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, spacing: 10, children: blocks),
    );
  }
}
