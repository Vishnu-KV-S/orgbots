import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api.dart';
import '../bot3d/snapshots.dart';
import '../conversation.dart';
import '../session.dart';
import '../theme.dart';
import 'cards.dart';
import 'rich_text.dart';
import 'work_block.dart';

const _quickReactions = ['👍', '👎', '❤️', '🎉'];

/// One row of the conversation: your message in a bubble, the bot's reply full-width
/// like a document, steps folded into a work block, and the cards that need you.
class ItemView extends StatefulWidget {
  const ItemView({
    super.key,
    required this.item,
    required this.bot,
    required this.live,
    required this.conversation,
  });

  final Item item;
  final Bot bot;

  /// This is the last item and the bot is still working on it.
  final bool live;
  final Conversation conversation;

  @override
  State<ItemView> createState() => _ItemViewState();
}

class _ItemViewState extends State<ItemView> {
  List<String>? _reactions;

  Future<void> _react(BotMessage m, String emoji) async {
    final mine = _reactions ?? m.reactions;
    try {
      final result = await SessionScope.read(context).api
          .react(widget.bot.id, m.id, emoji, !mine.contains(emoji));
      if (mounted) setState(() => _reactions = result);
    } on ApiError {
      // A reaction that did not save just does not show.
    }
  }

  void _menu(BotMessage m) {
    HapticFeedback.mediumImpact();
    showModalBottomSheet<void>(
      context: context,
      builder: (context) {
        final p = Palette.of(context);
        return SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                children: [
                  for (final e in _quickReactions)
                    InkWell(
                      borderRadius: BorderRadius.circular(Radii.pill),
                      onTap: () {
                        Navigator.pop(context);
                        _react(m, e);
                      },
                      child: Container(
                        padding: const EdgeInsets.all(12),
                        decoration: BoxDecoration(color: p.raised, shape: BoxShape.circle),
                        child: Text(e, style: const TextStyle(fontSize: 24)),
                      ),
                    ),
                ],
              ),
              const SizedBox(height: 8),
              ListTile(
                leading: const Icon(Icons.copy_rounded),
                title: const Text('Copy text'),
                onTap: () {
                  Clipboard.setData(ClipboardData(text: m.content));
                  Navigator.pop(context);
                },
              ),
              const SizedBox(height: 8),
            ],
          ),
        );
      },
    );
  }

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final item = widget.item;
    final convo = widget.conversation;
    final bot = widget.bot;
    if (item is WorkItem) {
      return WorkBlock(steps: item.steps, live: widget.live);
    }
    final m = (item as MessageItem).message;

    switch (m.role) {
      case 'user':
        final label = m.fromName.isNotEmpty
            ? m.fromName
            : m.routine.isNotEmpty
            ? 'Routine · ${m.routine}'
            : null;
        return Padding(
          padding: const EdgeInsets.only(left: 48),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.end,
            spacing: 6,
            children: [
              if (label != null)
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: Text(label, style: TextStyle(color: p.textFaint, fontSize: 11.5)),
                ),
              if (m.attachments.isNotEmpty)
                Wrap(
                  alignment: WrapAlignment.end,
                  spacing: 6,
                  runSpacing: 6,
                  children: [
                    for (final a in m.attachments)
                      Container(
                        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
                        constraints: const BoxConstraints(maxWidth: 220),
                        decoration: BoxDecoration(
                          color: p.bubble,
                          borderRadius: BorderRadius.circular(Radii.md),
                        ),
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          spacing: 6,
                          children: [
                            Icon(
                              a.mediaType.startsWith('image/')
                                  ? Icons.image_outlined
                                  : Icons.description_outlined,
                              size: 15,
                              color: p.textDim,
                            ),
                            Flexible(
                              child: Text(
                                a.name,
                                overflow: TextOverflow.ellipsis,
                                style: TextStyle(color: p.text, fontSize: 13),
                              ),
                            ),
                          ],
                        ),
                      ),
                  ],
                ),
              if (m.content.isNotEmpty)
                GestureDetector(
                  onLongPress: () => _menu(m),
                  child: Container(
                    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 11),
                    decoration: BoxDecoration(
                      color: p.bubble,
                      borderRadius: const BorderRadius.only(
                        topLeft: Radius.circular(Radii.lg),
                        topRight: Radius.circular(Radii.lg),
                        bottomLeft: Radius.circular(Radii.lg),
                        bottomRight: Radius.circular(6),
                      ),
                    ),
                    child: Text(
                      m.content,
                      style: TextStyle(color: p.text, fontSize: 16, height: 1.45),
                    ),
                  ),
                ),
            ],
          ),
        );
      case 'bot':
        final reactions = _reactions ?? m.reactions;
        return GestureDetector(
          onLongPress: () => _menu(m),
          behavior: HitTestBehavior.opaque,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            spacing: 10,
            children: [
              Row(
                spacing: 8,
                children: [
                  BotAvatar(botId: bot.id, appearance: bot.appearance, size: 26, mood: 'happy'),
                  Text(
                    bot.name,
                    style: TextStyle(color: p.textDim, fontSize: 13.5, fontWeight: FontWeight.w600),
                  ),
                ],
              ),
              BotText(m.content),
              if (m.screenshotId.isNotEmpty)
                ScreenshotThumb(botId: bot.id, screenshotId: m.screenshotId),
              if (reactions.isNotEmpty)
                Wrap(
                  spacing: 6,
                  children: [
                    for (final e in reactions)
                      Semantics(
                        button: true,
                        label: 'Remove reaction $e',
                        excludeSemantics: true,
                        child: GestureDetector(
                          onTap: () => _react(m, e),
                          child: Container(
                            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                            decoration: BoxDecoration(
                              color: p.raised,
                              borderRadius: BorderRadius.circular(Radii.pill),
                            ),
                            child: Text(e),
                          ),
                        ),
                      ),
                  ],
                ),
            ],
          ),
        );
      case 'approval':
        return ApprovalCard(
          botId: bot.id,
          message: m,
          live: convo.pending.contains(m.pendingId),
          onDecided: convo.poke,
        );
      case 'credentials':
        return CredentialCard(
          botId: bot.id,
          botName: bot.name,
          message: m,
          live: convo.asking.contains(m.credentialRequestId),
          onDone: convo.poke,
        );
      case 'error':
        return Container(
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: p.surface,
            border: Border.all(color: p.lineStrong),
            borderRadius: BorderRadius.circular(Radii.md),
          ),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            spacing: 10,
            children: [
              Icon(Icons.error_outline, size: 19, color: p.text),
              Expanded(
                child: Text(
                  m.content,
                  style: TextStyle(color: p.text, fontSize: 13.5, height: 1.4),
                ),
              ),
            ],
          ),
        );
      default:
        if (m.content.isEmpty) return const SizedBox.shrink();
        return Padding(
          padding: const EdgeInsets.symmetric(horizontal: 24),
          child: Text(
            m.content,
            textAlign: TextAlign.center,
            style: TextStyle(color: p.textFaint, fontSize: 13.5),
          ),
        );
    }
  }
}
