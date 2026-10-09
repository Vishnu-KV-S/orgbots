import 'dart:async';

import 'package:flutter/material.dart';

import '../api.dart';
import '../appearance.dart';
import '../bot3d/snapshots.dart';
import '../session.dart';
import '../text.dart';
import '../theme.dart';
import '../widgets/common.dart';
import 'chat.dart';
import 'new_bot.dart';
import 'settings.dart';

/// Home: every bot, most recent first, the ones waiting on you on top — the phone's
/// version of the web app's sidebar. Refreshed while it is on screen and the app is
/// in front, so "working", unread and "waiting for you" stay live.
class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> with WidgetsBindingObserver {
  List<Bot>? _bots;
  String? _error;
  String _query = '';
  Timer? _timer;

  Api get _api => SessionScope.read(context).api;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    // After the first frame: `_load` asks whether this route is in front.
    WidgetsBinding.instance.addPostFrameCallback((_) => _load());
    _start();
  }

  void _start() {
    _timer?.cancel();
    _timer = Timer.periodic(const Duration(seconds: 4), (_) => _load());
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      _load();
      _start();
    } else {
      _timer?.cancel();
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    // Only the screen in front polls.
    if (!mounted || !(ModalRoute.of(context)?.isCurrent ?? true)) return;
    try {
      final bots = await _api.listBots();
      if (mounted) {
        setState(() {
          _bots = bots;
          _error = null;
        });
      }
    } on ApiError catch (e) {
      if (mounted) setState(() => _error = e.message);
    }
  }

  List<Bot> get _visible {
    final q = _query.trim().toLowerCase();
    final list = (_bots ?? []).where((b) {
      if (b.hidden) return false;
      if (q.isEmpty) return true;
      return b.name.toLowerCase().contains(q) ||
          b.label.toLowerCase().contains(q) ||
          (b.lastMessage?.content ?? '').toLowerCase().contains(q);
    }).toList();
    list.sort((a, b) {
      if (a.needsAttention != b.needsAttention) {
        return a.needsAttention ? -1 : 1;
      }
      if (a.pinned != b.pinned) return a.pinned ? -1 : 1;
      return b.lastActive.compareTo(a.lastActive);
    });
    return list;
  }

  Future<void> _open(Widget screen) async {
    await Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => screen));
    await _load();
  }

  Future<void> _options(Bot bot) async {
    final p = Palette.of(context);
    final choice = await showModalBottomSheet<String>(
      context: context,
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              bot.name,
              style: TextStyle(color: p.text, fontSize: 17, fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 8),
            ListTile(
              leading: Icon(bot.pinned ? Icons.push_pin_outlined : Icons.push_pin),
              title: Text(bot.pinned ? 'Unpin' : 'Pin to top'),
              onTap: () => Navigator.pop(context, 'pin'),
            ),
            ListTile(
              leading: Icon(Icons.delete_outline, color: p.danger),
              title: Text('Delete', style: TextStyle(color: p.danger)),
              onTap: () => Navigator.pop(context, 'delete'),
            ),
            const SizedBox(height: 8),
          ],
        ),
      ),
    );
    if (!mounted) return;
    if (choice == 'pin') {
      setState(
        () => _bots = [for (final b in _bots!) b.id == bot.id ? b.copyWith(pinned: !b.pinned) : b],
      );
      try {
        await _api.updateBot(bot.id, {'pinned': !bot.pinned});
      } on ApiError catch (e) {
        _toast('Couldn’t change: ${e.message}');
      }
    } else if (choice == 'delete') {
      final sure = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: Text('Delete ${bot.name}?'),
          content: const Text(
            'Its conversation, memory and routines go with it. Helpers move up a level.',
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            TextButton(
              onPressed: () => Navigator.pop(context, true),
              child: Text('Delete', style: TextStyle(color: p.danger)),
            ),
          ],
        ),
      );
      if (sure != true) return;
      try {
        await _api.deleteBot(bot.id);
        await _load();
      } on ApiError catch (e) {
        _toast('Couldn’t delete: ${e.message}');
      }
    }
  }

  void _toast(String text) =>
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final session = SessionScope.of(context);
    final bots = _bots;
    final names = {for (final b in bots ?? <Bot>[]) b.id: b.name};
    final visible = _visible;

    return Scaffold(
      body: Readable(
        child: SafeArea(
          bottom: false,
          child: Column(
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(10, 4, 6, 4),
                child: Row(
                  children: [
                    // A labelled, 44-point target around the 36-point avatar.
                    Tooltip(
                      message: 'Settings',
                      child: InkResponse(
                        radius: 24,
                        onTap: () => _open(const SettingsScreen()),
                        child: Padding(
                          padding: const EdgeInsets.all(6),
                          child: Container(
                            width: 36,
                            height: 36,
                            alignment: Alignment.center,
                            decoration: BoxDecoration(
                              color: p.raised,
                              shape: BoxShape.circle,
                              border: Border.all(color: p.line),
                            ),
                            child: session.me == null
                                ? Icon(Icons.settings_outlined, size: 19, color: p.text)
                                : Text(
                                    session.me!.initial,
                                    style: TextStyle(
                                      color: p.text,
                                      fontWeight: FontWeight.w700,
                                      fontSize: 16,
                                    ),
                                  ),
                          ),
                        ),
                      ),
                    ),
                    Expanded(
                      child: Text(
                        'Orgbots',
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          color: p.text,
                          fontSize: 20,
                          fontWeight: FontWeight.w800,
                          letterSpacing: -0.4,
                        ),
                      ),
                    ),
                    RoundIcon(
                      icon: Icons.edit_square,
                      tooltip: 'New bot',
                      onPressed: () => _open(const NewBotScreen()),
                    ),
                  ],
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 6, 16, 8),
                child: TextField(
                  onChanged: (v) => setState(() => _query = v),
                  textInputAction: TextInputAction.search,
                  style: TextStyle(color: p.text, fontSize: 16),
                  decoration: fieldDecoration(context, hint: 'Search bots and messages', pill: true)
                      .copyWith(
                        prefixIcon: Icon(Icons.search, color: p.textFaint, size: 20),
                        contentPadding: const EdgeInsets.symmetric(vertical: 11),
                      ),
                ),
              ),
              if (session.status == Status.offline)
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
                  child: Column(
                    spacing: 8,
                    children: [
                      const Notice('Can’t reach your server right now.'),
                      PillButton(
                        label: 'Try again',
                        kind: ButtonKind.secondary,
                        onPressed: () async {
                          await session.refresh();
                          await _load();
                        },
                      ),
                    ],
                  ),
                )
              else if (_error != null && bots != null)
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16),
                  child: Notice(_error!),
                ),
              Expanded(
                child: bots == null
                    ? Center(
                        child: _error == null
                            ? const CircularProgressIndicator()
                            : Padding(
                                padding: const EdgeInsets.all(16),
                                child: Column(
                                  mainAxisSize: MainAxisSize.min,
                                  spacing: 10,
                                  children: [
                                    Notice(_error!),
                                    PillButton(
                                      label: 'Try again',
                                      kind: ButtonKind.secondary,
                                      onPressed: _load,
                                    ),
                                  ],
                                ),
                              ),
                      )
                    : RefreshIndicator(
                        onRefresh: _load,
                        color: p.text,
                        backgroundColor: p.raised,
                        child: visible.isEmpty
                            ? ListView(
                                children: [
                                  if (_query.isNotEmpty)
                                    Padding(
                                      padding: const EdgeInsets.only(top: 48),
                                      child: Text(
                                        'Nothing matches “$_query”.',
                                        textAlign: TextAlign.center,
                                        style: TextStyle(color: p.textFaint, fontSize: 16),
                                      ),
                                    )
                                  else
                                    _EmptyState(onCreate: () => _open(const NewBotScreen())),
                                ],
                              )
                            : ListView.builder(
                                padding: EdgeInsets.only(
                                  bottom: MediaQuery.paddingOf(context).bottom + 100,
                                ),
                                itemCount: visible.length,
                                itemBuilder: (context, i) {
                                  final bot = visible[i];
                                  return _BotRow(
                                    bot: bot,
                                    parent: bot.parentBotId == null ? null : names[bot.parentBotId],
                                    onTap: () {
                                      tap();
                                      _open(ChatScreen(bot: bot));
                                    },
                                    onLongPress: () => _options(bot),
                                  );
                                },
                              ),
                      ),
              ),
            ],
          ),
        ),
      ),
      floatingActionButtonLocation: FloatingActionButtonLocation.centerFloat,
      floatingActionButton: (bots?.isNotEmpty ?? false)
          ? FloatingActionButton.extended(
              onPressed: () {
                tap();
                _open(const NewBotScreen());
              },
              backgroundColor: p.accent,
              foregroundColor: p.onAccent,
              elevation: 6,
              shape: const StadiumBorder(),
              icon: const Icon(Icons.add),
              label: const Text(
                'New bot',
                style: TextStyle(fontWeight: FontWeight.w700, fontSize: 16),
              ),
            )
          : null,
    );
  }
}

class _BotRow extends StatelessWidget {
  const _BotRow({
    required this.bot,
    required this.parent,
    required this.onTap,
    required this.onLongPress,
  });
  final Bot bot;
  final String? parent;
  final VoidCallback onTap;
  final VoidCallback onLongPress;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final last = bot.lastMessage;
    final subtitle = bot.needsAttention
        ? 'Waiting for you'
        : bot.working
        ? 'Working…'
        : last != null
        ? '${last.role == 'user' ? 'You: ' : ''}${preview(last.content)}'
        : (bot.label.isNotEmpty
              ? bot.label
              : (bot.description.isNotEmpty ? bot.description : 'Say hello'));
    final emphasis = bot.needsAttention || bot.unread;
    return InkWell(
      onTap: onTap,
      onLongPress: onLongPress,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 9),
        child: Row(
          children: [
            BotAvatar(
              botId: bot.id,
              appearance: bot.appearance,
              mood: moodOfBot(bot),
              size: 56,
              working: bot.working,
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                spacing: 3,
                children: [
                  Row(
                    spacing: 6,
                    children: [
                      // The name gets all the room the time does not need; its icons
                      // follow it rather than drifting to the right edge.
                      Expanded(
                        child: Row(
                          spacing: 6,
                          children: [
                            Flexible(
                              child: Text(
                                bot.name,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: TextStyle(
                                  color: p.text,
                                  fontSize: 16,
                                  fontWeight: bot.unread ? FontWeight.w700 : FontWeight.w600,
                                ),
                              ),
                            ),
                            if (bot.pinned) Icon(Icons.push_pin, size: 13, color: p.textFaint),
                            if (bot.visibility == 'team')
                              Icon(Icons.group, size: 14, color: p.textFaint),
                          ],
                        ),
                      ),
                      if (last != null)
                        Text(
                          timeAgo(last.createdAt),
                          style: TextStyle(color: p.textFaint, fontSize: 13),
                        ),
                    ],
                  ),
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          '${parent != null ? '$parent’s helper · ' : ''}$subtitle',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            color: emphasis ? p.text : p.textDim,
                            fontSize: 14,
                            fontWeight: bot.needsAttention ? FontWeight.w700 : FontWeight.w400,
                          ),
                        ),
                      ),
                      if (bot.needsAttention)
                        Container(
                          width: 20,
                          height: 20,
                          alignment: Alignment.center,
                          decoration: BoxDecoration(color: p.accent, shape: BoxShape.circle),
                          child: Text(
                            '!',
                            style: TextStyle(
                              color: p.onAccent,
                              fontWeight: FontWeight.w800,
                              fontSize: 13,
                            ),
                          ),
                        )
                      else if (bot.unread)
                        Container(
                          width: 9,
                          height: 9,
                          decoration: BoxDecoration(color: p.accent, shape: BoxShape.circle),
                        ),
                    ],
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({required this.onCreate});
  final VoidCallback onCreate;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(32, 64, 32, 32),
      child: Column(
        spacing: 14,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              BotAvatar(botId: 'sprout', appearance: presets['Sprout']!, size: 64),
              BotAvatar(botId: 'telly', appearance: presets['Telly']!, size: 96, mood: 'happy'),
              BotAvatar(botId: 'beacon', appearance: presets['Beacon']!, size: 64),
            ],
          ),
          Text(
            'Hire your first bot',
            style: TextStyle(color: p.text, fontSize: 24, fontWeight: FontWeight.w700),
          ),
          Text(
            'Each bot has its own browser on your server. Give it a job and it works on it, asking you before anything that matters.',
            textAlign: TextAlign.center,
            style: TextStyle(color: p.textDim, fontSize: 16, height: 1.45),
          ),
          const SizedBox(height: 4),
          PillButton(label: 'Create a bot', icon: Icons.add, onPressed: onCreate),
        ],
      ),
    );
  }
}
