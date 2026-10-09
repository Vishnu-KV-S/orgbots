import 'dart:async';

import 'package:flutter/widgets.dart';

import 'api.dart';

/// One bot's transcript, kept current by polling with a cursor — the web app's
/// contract: `seq` only grows, so each poll asks for what follows the last row it
/// has. Fast while the bot works, slow when idle, and paused while the app is in the
/// background so a pocketed phone does not keep the radio awake.
class Conversation extends ChangeNotifier with WidgetsBindingObserver {
  Conversation(this.api, this.botId) {
    WidgetsBinding.instance.addObserver(this);
    unawaited(_tick());
  }

  static const _fast = Duration(seconds: 1);
  static const _slow = Duration(seconds: 3);

  final Api api;
  final String botId;

  final List<BotMessage> messages = [];
  Set<String> pending = {};
  Set<String> asking = {};
  bool working = false;
  bool loaded = false;
  String? error;

  int _cursor = 0;
  Timer? _timer;
  bool _inflight = false;
  bool _active = true;
  bool _disposed = false;

  int get waiting => pending.length + asking.length;

  /// Fetch now, outside the timer — after sending, so the message appears at once.
  void poke() {
    _timer?.cancel();
    unawaited(_tick());
  }

  Future<void> _tick() async {
    if (_inflight || !_active || _disposed) return;
    _inflight = true;
    var next = _slow;
    try {
      final page = await api.fetchMessages(botId, _cursor);
      if (_disposed) return;
      if (page.messages.isNotEmpty) {
        _cursor = page.messages.last.seq;
        final seen = {for (final m in messages) m.id};
        messages.addAll(page.messages.where((m) => !seen.contains(m.id)));
      }
      pending = page.pending;
      asking = page.asking;
      working = page.working;
      error = null;
      loaded = true;
      next = page.working ? _fast : _slow;
      notifyListeners();
    } on ApiError catch (e) {
      if (_disposed) return;
      error = e.message;
      notifyListeners();
    } finally {
      _inflight = false;
      if (!_disposed && _active) {
        _timer?.cancel();
        _timer = Timer(next, () => unawaited(_tick()));
      }
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    final now = state == AppLifecycleState.resumed;
    if (now == _active) return;
    _active = now;
    _timer?.cancel();
    if (now) unawaited(_tick());
  }

  @override
  void dispose() {
    _disposed = true;
    _timer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }
}

/// Consecutive activity rows fold into one "worked" block, as on the web.
sealed class Item {
  const Item(this.id);
  final String id;
}

class MessageItem extends Item {
  MessageItem(this.message) : super(message.id);
  final BotMessage message;
}

class WorkItem extends Item {
  WorkItem(super.id, this.steps);
  final List<BotMessage> steps;
}

List<Item> group(List<BotMessage> messages) {
  final items = <Item>[];
  for (final m in messages) {
    if (m.role == 'activity') {
      final last = items.isEmpty ? null : items.last;
      if (last is WorkItem) {
        last.steps.add(m);
      } else {
        items.add(WorkItem(m.id, [m]));
      }
    } else {
      items.add(MessageItem(m));
    }
  }
  return items;
}
