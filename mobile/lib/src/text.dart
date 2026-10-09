import 'package:flutter/material.dart';

import 'api.dart';

/// One action, the way a person would say it. Mirrors `ui/features/bots/lib/text.tsx`.
String describeAction(Json action) {
  String s(String k) => action[k] is String ? action[k] as String : '';
  final label = s('element_label');
  final target = label.isNotEmpty
      ? '“$label”'
      : action['element'] != null
      ? '[${action['element']}]'
      : '';
  switch (s('type')) {
    case 'navigate':
      return 'Open ${s('url')}';
    case 'click':
      return 'Click $target';
    case 'type':
      final text = action['secret'] == true ? '••••••' : '“${s('text')}”';
      return 'Type $text into $target${action['submit'] == true ? ' and press Enter' : ''}';
    case 'press':
      return 'Press ${s('key')}';
    case 'select':
      return 'Choose “${s('option')}” in $target';
    case 'scroll':
      return 'Scroll ${s('direction').isEmpty ? 'down' : s('direction')}';
    case 'hover':
      return 'Hover over $target';
    case 'back':
      return 'Go back';
    case 'forward':
      return 'Go forward';
    case 'reload':
      return 'Reload the page';
    case 'wait':
      return 'Wait ${action['seconds'] ?? 1}s';
    case 'upload':
      return 'Upload a file';
    case 'remember':
      return s('bot').isNotEmpty ? 'Teach ${s('bot')}' : 'Save to memory';
    case 'forget':
      return 'Forget a memory';
    case 'recall':
      return 'Recall “${s('text')}”';
    case 'update_brief':
      return s('bot').isNotEmpty ? 'Update ${s('bot')}’s brief' : 'Update own brief';
    case 'create_bot':
      return 'Create helper “${s('bot')}”';
    case 'ask_bot':
      return 'Ask ${s('bot').isEmpty ? 'helper' : s('bot')}: ${s('text')}';
    case 'bot_answer':
      return '${s('bot').isEmpty ? 'Helper' : s('bot')} answered';
    case 'observe':
      return 'Look at the page';
    case 'plan':
      return 'Plan';
    case 'look':
      return 'Look at the screen: “${s('text')}”';
    case 'sign_in':
      return 'Sign in to ${s('host').isEmpty ? 'the site' : s('host')}';
    case 'list_files':
      return s('text').isNotEmpty
          ? 'Search team files for “${s('text')}”'
          : 'Look in the team files';
    case 'read_file':
      return 'Read ${s('path')}';
    case 'write_file':
      return 'Write ${s('path')}';
    case 'append_file':
      return 'Add to ${s('path')}';
    case 'edit_file':
      return 'Edit ${s('path')}';
    case 'move_file':
      return 'Move ${s('path')} to ${s('to')}';
    case 'copy_file':
      return 'Copy ${s('path')} to ${s('to')}';
    case 'delete_file':
      return 'Delete ${s('path')}';
    case 'use_connector':
      return 'Use ${s('text').isEmpty ? 'a connected app' : s('text')}';
    case 'review':
      final v = s('verdict');
      return 'Auto Review: ${v == 'allow'
          ? 'allowed'
          : v == 'deny'
          ? 'refused'
          : 'asked you'} — ${s('text')}';
    case 'run_command':
      return 'Run a command: ${s('text')}';
    case 'save_skill':
      return 'Save skill /${s('name')}';
    case 'use_skill':
      return 'Use skill /${s('name')}';
    case 'save_routine':
      return 'Save routine “${s('name')}”';
    case 'delete_routine':
      return 'Delete routine “${s('name')}”';
    default:
      return s('type');
  }
}

const actionIcons = <String, IconData>{
  'navigate': Icons.public,
  'click': Icons.ads_click,
  'type': Icons.keyboard_outlined,
  'press': Icons.keyboard_return,
  'select': Icons.unfold_more,
  'scroll': Icons.swap_vert,
  'hover': Icons.near_me_outlined,
  'back': Icons.arrow_back,
  'forward': Icons.arrow_forward,
  'reload': Icons.refresh,
  'wait': Icons.pause_circle_outline,
  'upload': Icons.upload_outlined,
  'remember': Icons.bookmark_add_outlined,
  'forget': Icons.bookmark_remove_outlined,
  'recall': Icons.menu_book_outlined,
  'update_brief': Icons.assignment_outlined,
  'create_bot': Icons.person_add_alt,
  'ask_bot': Icons.forum_outlined,
  'bot_answer': Icons.chat_bubble_outline,
  'observe': Icons.center_focus_weak,
  'plan': Icons.format_list_bulleted,
  'look': Icons.visibility_outlined,
  'sign_in': Icons.key_outlined,
  'list_files': Icons.folder_open_outlined,
  'read_file': Icons.description_outlined,
  'write_file': Icons.note_add_outlined,
  'append_file': Icons.post_add,
  'edit_file': Icons.edit_note,
  'move_file': Icons.drive_file_move_outline,
  'copy_file': Icons.file_copy_outlined,
  'delete_file': Icons.delete_outline,
  'review': Icons.verified_user_outlined,
  'use_connector': Icons.apps,
  'run_command': Icons.terminal,
  'save_skill': Icons.auto_awesome_outlined,
  'use_skill': Icons.auto_awesome_outlined,
  'save_routine': Icons.schedule,
  'delete_routine': Icons.schedule,
};

String timeAgo(DateTime when) {
  final seconds = DateTime.now().difference(when).inSeconds;
  if (seconds < 45) return 'now';
  if (seconds < 3600) return '${(seconds / 60).round()}m';
  if (seconds < 86400) return '${(seconds / 3600).round()}h';
  return '${(seconds / 86400).round()}d';
}

/// A one-line preview of a message for the bots list.
String preview(String content) =>
    content.replaceAll(RegExp(r'\*\*|`'), '').replaceAll(RegExp(r'\s+'), ' ').trim();

/// Starters on an empty conversation, in the spirit of the web app's `/` prompts.
const starters = [
  ('Research a topic', 'Research the following and report the 5 most useful sources, with links: '),
  ('Compare options', 'Compare these options and recommend one, with the trade-offs: '),
  ('Summarize a page', 'Open this page and summarize the key points in a short list: '),
  ('Progress report', 'Where are you up to? Give me a short progress report.'),
];

/// Starting points for a new bot — the web app's templates.
final botTemplates =
    <
      ({
        String name,
        String label,
        String blurb,
        String avatar,
        String preset,
        String description,
        Json brief,
      })
    >[
      (
        name: 'Researcher',
        label: 'Web research',
        blurb: 'Find and compare information, with sources',
        avatar: '🔎',
        preset: 'Orbit',
        description: 'Finds, reads and compares sources on the web, and reports back with links.',
        brief: {
          'mission': 'Answer my research questions with well-sourced, current information.',
          'duties': [
            'Find and read primary sources',
            'Compare options with their trade-offs',
            'Report back with links for every claim',
          ],
          'boundaries': <String>[],
          'style': 'Short, table-like lists. Lead with the answer.',
          'escalation': 'Ask me when sources disagree on something that matters.',
          'notes': '',
        },
      ),
      (
        name: 'Shopper',
        label: 'Price hunter',
        blurb: 'Compare products and prices across stores',
        avatar: '🛒',
        preset: 'Cubey',
        description: 'Searches stores for products, compares prices and availability.',
        brief: {
          'mission': 'Find the best price for what I want to buy.',
          'duties': [
            'Search several stores',
            'Check availability and delivery',
            'Report the store, price and link',
          ],
          'boundaries': ['Never place an order or enter payment details without asking me first'],
          'style': '',
          'escalation': '',
          'notes': '',
        },
      ),
      (
        name: 'Inbox assistant',
        label: 'Email & messages',
        blurb: 'Triage and draft replies in web mail',
        avatar: '✉️',
        preset: 'Beacon',
        description: 'Reads and drafts messages in web mail once you have signed in for it.',
        brief: {
          'mission': 'Keep my inbox under control.',
          'duties': ['Triage new mail by urgency', 'Draft replies to what needs one'],
          'boundaries': ['Never send anything without my approval'],
          'style': 'Drafts are short and in my voice.',
          'escalation': '',
          'notes': '',
        },
      ),
      (
        name: 'Social manager',
        label: 'Social media',
        blurb: 'Watch mentions and draft posts',
        avatar: '📣',
        preset: 'Telly',
        description: 'Monitors mentions and drafts posts for your social accounts.',
        brief: {
          'mission': 'Look after my social media presence.',
          'duties': ['Watch mentions and replies', 'Draft posts for my approval'],
          'boundaries': ['Never post publicly without asking me first'],
          'style': '',
          'escalation': '',
          'notes': '',
        },
      ),
      (
        name: 'Assistant',
        label: '',
        blurb: 'A blank bot you shape yourself',
        avatar: '🤖',
        preset: 'Sprout',
        description: '',
        brief: {
          'mission': '',
          'duties': <String>[],
          'boundaries': <String>[],
          'style': '',
          'escalation': '',
          'notes': '',
        },
      ),
    ];
