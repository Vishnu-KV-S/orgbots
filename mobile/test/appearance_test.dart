import 'package:flutter_test/flutter_test.dart';
import 'package:orgbots/src/api.dart';
import 'package:orgbots/src/appearance.dart';

/// Expected values are `randomAppearance(id)` from `ui/features/bots/avatar/appearance.ts`:
/// a bot that saved no look must get the same one on the phone as in the browser.
void main() {
  const expected = {
    'b1': {
      'shape': 'orb',
      'body': '#c9b8f0',
      'glow': '#7c4dff',
      'eyes': 'square',
      'top': 'ring',
      'finish': 'metal',
    },
    'anonymous': {
      'shape': 'pod',
      'body': '#f2c6d9',
      'glow': '#ffffff',
      'eyes': 'dot',
      'top': 'knobs',
      'finish': 'matte',
    },
    '3f6c2a1e-9b7d-4c1a-8e2f-0a1b2c3d4e5f': {
      'shape': 'pod',
      'body': '#b9e3f5',
      'glow': '#ff5a4f',
      'eyes': 'pill',
      'top': 'ring',
      'finish': 'matte',
    },
    'Price Tracker': {
      'shape': 'orb',
      'body': '#2c2f3a',
      'glow': '#ffb02e',
      'eyes': 'dot',
      'top': 'antenna',
      'finish': 'matte',
    },
    'ünïcödé 🤖': {
      'shape': 'orb',
      'body': '#e9b06b',
      'glow': '#c6ff3d',
      'eyes': 'visor',
      'top': 'halo',
      'finish': 'matte',
    },
  };

  expected.forEach((id, look) {
    test('derives the web app\'s look for "$id"', () {
      expect(randomAppearance(id), look);
    });
  });

  test('a saved look wins over the derived one, field by field', () {
    final look = appearanceFor('b1', {'glow': '#18c8ff'});
    expect(look['glow'], '#18c8ff');
    expect(look['shape'], 'orb');
  });

  group('moods', () {
    Bot bot({
      bool working = false,
      bool needs = false,
      String? lastRole,
      Duration ago = Duration.zero,
    }) => Bot.fromJson({
      'id': 'x',
      'working': working,
      'needs_attention': needs,
      'updated_at': DateTime.now().subtract(ago).toIso8601String(),
      'last_message': lastRole == null
          ? null
          : {
              'id': 'm',
              'seq': 1,
              'role': lastRole,
              'content': '',
              'created_at': DateTime.now().subtract(ago).toIso8601String(),
            },
    });

    test('working thinks; waiting on you wins over an old reply', () {
      expect(moodOfBot(bot(working: true)), 'thinking');
      expect(
        moodOfBot(bot(needs: true, lastRole: 'bot', ago: const Duration(hours: 1))),
        'waiting',
      );
    });

    test('a fresh reply is happy, a quiet bot sleeps', () {
      expect(moodOfBot(bot(lastRole: 'bot')), 'happy');
      expect(moodOfBot(bot(lastRole: 'bot', ago: const Duration(hours: 7))), 'sleeping');
      expect(moodOfBot(bot(lastRole: 'bot', ago: const Duration(minutes: 5))), 'idle');
    });

    test('a conversation mood follows the last step', () {
      BotMessage step(String type, {bool ok = true}) => BotMessage.fromJson({
        'id': type,
        'seq': 1,
        'role': 'activity',
        'content': '',
        'payload': {
          'action': {'type': type},
          'ok': ok,
        },
        'created_at': DateTime.now().toIso8601String(),
      });
      expect(moodOfConversation([step('navigate')], working: true, waiting: 0), 'browsing');
      expect(moodOfConversation([step('type')], working: true, waiting: 0), 'typing');
      expect(moodOfConversation([step('click', ok: false)], working: true, waiting: 0), 'error');
      expect(moodOfConversation([step('click')], working: true, waiting: 1), 'waiting');
      expect(moodOfConversation([], working: false, waiting: 0), 'idle');
    });
  });

  test('server addresses are normalised as typed', () {
    expect(normalizeServer('bots.example.com'), 'https://bots.example.com');
    expect(normalizeServer('http://192.168.1.5:3000/some/path'), 'http://192.168.1.5:3000');
    expect(normalizeServer('  '), isNull);
  });
}
