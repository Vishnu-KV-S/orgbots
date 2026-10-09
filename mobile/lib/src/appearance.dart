import 'api.dart';

/// A bot's look and mood, as `ui/features/bots/avatar/appearance.ts` and `mood.ts`
/// define them. The 3D renderer derives the look itself; this port exists for the
/// flat fallback face and the snapshot cache key, so it has to give the same answer
/// bit for bit — `test/appearance_test.dart` checks it against the web's output.

const shapes = ['orb', 'cube', 'capsule', 'pod', 'tv'];
const eyes = ['pill', 'round', 'square', 'visor', 'dot'];
const tops = ['ring', 'knobs', 'antenna', 'ears', 'halo', 'none'];
const finishes = ['gloss', 'pearl', 'metal', 'matte'];

const bodyColors = [
  '#ecebe7', '#eceaf3', '#c9b8f0', '#9aa3b5', '#2c2f3a', '#f2c6d9', //
  '#b9e3f5', '#cfe8c4', '#f5d9a8', '#e9b06b', '#7a5cff',
];

const glowColors = [
  '#f5a623', '#e040fb', '#ff4fa3', '#7c4dff', '#18c8ff', //
  '#3dffb0', '#c6ff3d', '#ffb02e', '#ff5a4f', '#ffffff',
];

/// The web app's starting looks.
const presets = <String, Json>{
  'Orbit': {
    'shape': 'orb',
    'body': '#ecebe7',
    'glow': '#f5a623',
    'eyes': 'pill',
    'top': 'ring',
    'finish': 'matte',
  },
  'Cubey': {
    'shape': 'cube',
    'body': '#d8d4cc',
    'glow': '#ff4fa3',
    'eyes': 'pill',
    'top': 'knobs',
    'finish': 'matte',
  },
  'Sprout': {
    'shape': 'capsule',
    'body': '#cfe0c8',
    'glow': '#3dffb0',
    'eyes': 'pill',
    'top': 'antenna',
    'finish': 'matte',
  },
  'Beacon': {
    'shape': 'pod',
    'body': '#3a3b3f',
    'glow': '#18c8ff',
    'eyes': 'pill',
    'top': 'halo',
    'finish': 'matte',
  },
  'Telly': {
    'shape': 'tv',
    'body': '#e9dfcf',
    'glow': '#f5a623',
    'eyes': 'pill',
    'top': 'ears',
    'finish': 'matte',
  },
};

const _two32 = 4294967296;

/// `Math.imul(a, b) >>> 0`, without leaving the range where Dart-on-web ints are exact.
int _imul(int a, int b) {
  final ah = (a ~/ 65536) % 65536, al = a % 65536;
  final bh = (b ~/ 65536) % 65536, bl = b % 65536;
  return (al * bl + ((ah * bl + al * bh) % 65536) * 65536) % _two32;
}

int _xor(int a, int b) => (a ^ b) % _two32;

/// FNV-1a over UTF-16 code units, as `hash()` on the web.
int _hash(String text) {
  var h = 2166136261;
  for (final unit in text.codeUnits) {
    h = _imul(_xor(h, unit), 16777619);
  }
  return h;
}

/// mulberry32, as `rng()` on the web.
double Function() _rng(int seed) {
  var a = seed % _two32;
  return () {
    a = (a + 0x6d2b79f5) % _two32;
    var t = a;
    t = _imul(_xor(t, t ~/ 32768), t | 1);
    t = _xor(t, (t + _imul(_xor(t, t ~/ 128), t | 61)) % _two32);
    return _xor(t, t ~/ 16384) / _two32;
  };
}

T _pick<T>(double Function() r, List<T> list) => list[(r() * list.length).floor()];

Json randomAppearance(String seed) {
  final r = _rng(_hash(seed));
  return {
    'shape': _pick(r, shapes),
    'body': _pick(r, bodyColors),
    'glow': _pick(r, glowColors),
    'eyes': _pick(r, eyes),
    'top': _pick(r, tops),
    'finish': _pick(r, finishes),
  };
}

/// What to draw for a bot: what it saved, filled in from its id where it saved nothing.
Json appearanceFor(String id, Json saved) =>
    saved.isEmpty ? randomAppearance(id) : {...randomAppearance(id), ...saved};

// --- moods -------------------------------------------------------------------------

/// What the bot's body is doing; each has its own animation in the 3D model.
const _actionMood = {
  'navigate': 'browsing',
  'scroll': 'browsing',
  'back': 'browsing',
  'forward': 'browsing',
  'reload': 'browsing',
  'observe': 'browsing',
  'hover': 'browsing',
  'wait': 'thinking',
  'click': 'clicking',
  'select': 'clicking',
  'press': 'clicking',
  'type': 'typing',
  'remember': 'remembering',
  'create_bot': 'creating',
  'ask_bot': 'delegating',
  'bot_answer': 'happy',
};

const _happyFor = Duration(seconds: 12);
const _asleepAfter = Duration(hours: 6);

/// For a list row, from the summary the bot list carries.
String moodOfBot(Bot bot, [DateTime? now]) {
  now ??= DateTime.now();
  final last = bot.lastMessage;
  if (bot.working) return 'thinking';
  if (last?.role == 'approval' || bot.needsAttention) return 'waiting';
  if (last?.role == 'error') return 'error';
  if (last?.role == 'bot' && now.difference(last!.createdAt) < _happyFor) {
    return 'happy';
  }
  if (bot.hidden || now.difference(bot.lastActive) > _asleepAfter) {
    return 'sleeping';
  }
  return 'idle';
}

/// For an open conversation, from the transcript, which says exactly what it is doing.
String moodOfConversation(
  List<BotMessage> messages, {
  required bool working,
  required int waiting,
  DateTime? now,
}) {
  now ??= DateTime.now();
  if (waiting > 0) return 'waiting';
  final last = messages.isEmpty ? null : messages.last;
  if (working) {
    if (last == null || last.role == 'user') return 'thinking';
    if (last.role == 'activity') {
      if (last.ok == false) return 'error';
      return _actionMood[last.actionType] ?? 'thinking';
    }
    return 'thinking';
  }
  if (last == null) return 'idle';
  if (last.role == 'error') return 'error';
  if (last.role == 'system' && RegExp('stop', caseSensitive: false).hasMatch(last.content)) {
    return 'stopped';
  }
  if (last.role == 'system' && last.payload['controller'] == 'human') {
    return 'waiting';
  }
  if (last.role == 'bot') {
    if (last.payload['kind'] == 'ask_user') return 'waiting';
    if (now.difference(last.createdAt) < _happyFor) return 'happy';
  }
  if (now.difference(last.createdAt) > _asleepAfter) return 'sleeping';
  return 'idle';
}
