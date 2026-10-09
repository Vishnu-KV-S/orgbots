import 'dart:async';

import 'package:flutter/material.dart';

import '../api.dart';
import '../appearance.dart';
import '../bot3d/bot_view.dart';
import '../conversation.dart';
import '../session.dart';
import '../text.dart';
import '../theme.dart';
import '../widgets/common.dart';
import '../widgets/composer.dart';
import '../widgets/message_view.dart';
import 'live_screen.dart';

/// Talking to one bot. Its 3D body sits in the header and acts out what it is doing —
/// browsing, typing, thinking, waiting on you — exactly as on the web. Replies read
/// full-width, your messages sit in bubbles, steps fold into a live "Working" line,
/// and anything the bot needs from you appears as a card in the conversation.
class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key, required this.bot});
  final Bot bot;

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  late Bot _bot = widget.bot;
  late final Conversation _convo;
  final _draft = TextEditingController();
  String? _sendError;
  int _seen = -1;

  Api get _api => SessionScope.read(context).api;

  @override
  void initState() {
    super.initState();
    final api = SessionScope.read(context).api;
    _convo = Conversation(api, widget.bot.id)..addListener(_changed);
    unawaited(
      api
          .getBot(widget.bot.id)
          .then((b) => mounted ? setState(() => _bot = b) : null, onError: (_) {}),
    );
  }

  void _changed() {
    // Reading the conversation is reading it: clear the unread dot as messages land.
    if (_convo.loaded && _convo.messages.length != _seen) {
      _seen = _convo.messages.length;
      unawaited(_api.markRead(_bot.id).catchError((_) {}));
    }
    setState(() {});
  }

  @override
  void dispose() {
    _convo
      ..removeListener(_changed)
      ..dispose();
    _draft.dispose();
    super.dispose();
  }

  Future<bool> _send(String text, List<Attachment> attachments) async {
    setState(() => _sendError = null);
    try {
      final sent = await _api.sendMessage(_bot.id, text, [for (final a in attachments) a.id]);
      if (!sent.admitted && sent.refusalReason != null) {
        setState(() => _sendError = sent.refusalReason);
      }
      _convo.poke();
      return true;
    } on ApiError catch (e) {
      setState(() => _sendError = e.message);
      return false;
    }
  }

  Future<void> _stop() async {
    try {
      await _api.stopBot(_bot.id);
      _convo.poke();
    } on ApiError catch (e) {
      setState(() => _sendError = e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final convo = _convo;
    final items = group(convo.messages);
    final waiting = convo.waiting > 0;
    final mood = moodOfConversation(convo.messages, working: convo.working, waiting: convo.waiting);
    final status = waiting
        ? 'Needs you'
        : convo.working
        ? 'Working…'
        : (_bot.label.isNotEmpty ? _bot.label : 'Online');
    final empty = convo.loaded && items.isEmpty;
    final thinking = convo.working && !waiting && (items.isEmpty || items.last is! WorkItem);

    return Scaffold(
      resizeToAvoidBottomInset: true,
      appBar: AppBar(
        titleSpacing: 0,
        toolbarHeight: 64,
        title: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Bot3D(
              botId: _bot.id,
              appearance: _bot.appearance,
              mood: mood,
              size: 52,
              framing: 'head',
            ),
            const SizedBox(width: 6),
            Flexible(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    _bot.name,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: p.text, fontSize: 17, fontWeight: FontWeight.w700),
                  ),
                  Text(
                    status,
                    style: TextStyle(color: waiting ? p.text : p.textFaint, fontSize: 12),
                  ),
                ],
              ),
            ),
          ],
        ),
        actions: [
          IconButton(
            tooltip: 'Watch ${_bot.name}’s screen',
            icon: const Icon(Icons.desktop_windows_outlined),
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute<void>(
                fullscreenDialog: true,
                builder: (_) => LiveScreen(botId: _bot.id, botName: _bot.name),
              ),
            ),
          ),
        ],
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(1),
          child: Divider(height: 1, color: p.line),
        ),
      ),
      body: Column(
        children: [
          Expanded(
            child: !convo.loaded
                ? Center(
                    child: convo.error != null
                        ? Padding(padding: const EdgeInsets.all(24), child: Notice(convo.error!))
                        : const CircularProgressIndicator(),
                  )
                : empty
                ? _Welcome(bot: _bot, mood: mood)
                : ListView.separated(
                    reverse: true,
                    keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
                    padding: const EdgeInsets.fromLTRB(18, 20, 18, 20),
                    itemCount: items.length + (thinking ? 1 : 0),
                    separatorBuilder: (_, _) => const SizedBox(height: 18),
                    itemBuilder: (context, i) {
                      if (thinking && i == 0) return _Thinking(name: _bot.name);
                      final index = items.length - 1 - (i - (thinking ? 1 : 0));
                      final item = items[index];
                      return ItemView(
                        key: ValueKey(item.id),
                        item: item,
                        bot: _bot,
                        live: index == items.length - 1 && convo.working,
                        conversation: convo,
                      );
                    },
                  ),
          ),
          SafeArea(
            top: false,
            minimum: const EdgeInsets.only(bottom: 10),
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 6, 12, 0),
              child: Column(
                spacing: 8,
                children: [
                  if (_sendError != null)
                    GestureDetector(
                      onTap: () => setState(() => _sendError = null),
                      child: Notice(_sendError!),
                    ),
                  if (empty)
                    SizedBox(
                      height: 42,
                      child: ListView(
                        scrollDirection: Axis.horizontal,
                        children: [
                          for (final (title, text) in [
                            for (final d in _bot.duties.take(3)) (d, '$d: '),
                            ...starters,
                          ].take(5))
                            Padding(
                              padding: const EdgeInsets.only(right: 8),
                              child: OutlinedButton(
                                onPressed: () {
                                  tap();
                                  _draft
                                    ..text = text
                                    ..selection = TextSelection.collapsed(offset: text.length);
                                },
                                style: OutlinedButton.styleFrom(
                                  foregroundColor: p.text,
                                  side: BorderSide(color: p.lineStrong),
                                  shape: const StadiumBorder(),
                                  padding: const EdgeInsets.symmetric(horizontal: 16),
                                ),
                                child: Text(title, style: const TextStyle(fontSize: 14.5)),
                              ),
                            ),
                        ],
                      ),
                    ),
                  Composer(
                    controller: _draft,
                    botId: _bot.id,
                    botName: _bot.name,
                    working: convo.working,
                    onSend: _send,
                    onStop: _stop,
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _Welcome extends StatelessWidget {
  const _Welcome({required this.bot, required this.mood});
  final Bot bot;
  final String mood;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final lead = bot.mission.isNotEmpty ? bot.mission : bot.description;
    return SingleChildScrollView(
      padding: const EdgeInsets.all(32),
      child: Column(
        children: [
          const SizedBox(height: 24),
          Bot3D(botId: bot.id, appearance: bot.appearance, mood: mood, size: 220),
          const SizedBox(height: 8),
          Text(
            'What can I do for you?',
            textAlign: TextAlign.center,
            style: TextStyle(
              color: p.text,
              fontSize: 28,
              fontWeight: FontWeight.w700,
              letterSpacing: -0.5,
            ),
          ),
          if (lead.isNotEmpty) ...[
            const SizedBox(height: 12),
            Text(
              lead,
              textAlign: TextAlign.center,
              style: TextStyle(color: p.textDim, fontSize: 16, height: 1.45),
            ),
          ],
        ],
      ),
    );
  }
}

class _Thinking extends StatelessWidget {
  const _Thinking({required this.name});
  final String name;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Row(
      spacing: 10,
      children: [
        SizedBox.square(
          dimension: 14,
          child: CircularProgressIndicator(strokeWidth: 1.8, color: p.textFaint),
        ),
        Text('$name is thinking…', style: TextStyle(color: p.textFaint, fontSize: 13.5)),
      ],
    );
  }
}
