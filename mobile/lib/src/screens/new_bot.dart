import 'package:flutter/material.dart';

import '../api.dart';
import '../appearance.dart';
import '../bot3d/bot_view.dart';
import '../bot3d/snapshots.dart';
import '../session.dart';
import '../text.dart';
import '../theme.dart';
import '../widgets/common.dart';
import 'chat.dart';

/// Hire a bot: pick a starting point — its 3D body turns to greet you — name it and
/// say what it is for. Duties, boundaries and its look can be refined in the web app.
class NewBotScreen extends StatefulWidget {
  const NewBotScreen({super.key});

  @override
  State<NewBotScreen> createState() => _NewBotScreenState();
}

class _NewBotScreenState extends State<NewBotScreen> {
  int _chosen = 0;
  final _name = TextEditingController(text: botTemplates.first.name);
  final _mission = TextEditingController(text: botTemplates.first.brief['mission'] as String);
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _name.dispose();
    _mission.dispose();
    super.dispose();
  }

  void _choose(int i) {
    tap();
    setState(() {
      _chosen = i;
      _name.text = botTemplates[i].name;
      _mission.text = botTemplates[i].brief['mission'] as String;
    });
  }

  Future<void> _create() async {
    final t = botTemplates[_chosen];
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final bot = await SessionScope.read(context).api.createBot({
        'name': _name.text.trim(),
        'label': t.label,
        'description': t.description,
        'avatar': t.avatar,
        'appearance': presets[t.preset],
        'brief': {...t.brief, 'mission': _mission.text.trim()},
      });
      if (!mounted) return;
      await Navigator.of(context)
          .pushReplacement(MaterialPageRoute<void>(builder: (_) => ChatScreen(bot: bot)));
    } on ApiError catch (e) {
      if (mounted) {
        setState(() {
          _error = e.message;
          _busy = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final t = botTemplates[_chosen];
    return Scaffold(
      appBar: AppBar(
        centerTitle: false,
        automaticallyImplyLeading: false,
        title: Text(
          'New bot',
          style: TextStyle(fontSize: 28, fontWeight: FontWeight.w800, color: p.text),
        ),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 12),
            child: RoundIcon(
              icon: Icons.close,
              tooltip: 'Close',
              filled: true,
              onPressed: () => Navigator.pop(context),
            ),
          ),
        ],
      ),
      body: ListView(
        padding: EdgeInsets.fromLTRB(20, 0, 20, MediaQuery.paddingOf(context).bottom + 24),
        keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
        children: [
          Center(
            child: Bot3D(
              key: const ValueKey('preview'),
              botId: 'new-${t.preset}',
              appearance: presets[t.preset]!,
              mood: 'happy',
              size: 170,
            ),
          ),
          const SectionLabel('Start from'),
          GridView.count(
            crossAxisCount: 2,
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            mainAxisSpacing: 10,
            crossAxisSpacing: 10,
            childAspectRatio: 1.25,
            children: [
              for (var i = 0; i < botTemplates.length; i++)
                _TemplateCard(selected: i == _chosen, index: i, onTap: () => _choose(i)),
            ],
          ),
          const SectionLabel('Name'),
          TextField(
            controller: _name,
            maxLength: 60,
            onChanged: (_) => setState(() {}),
            style: TextStyle(color: p.text, fontSize: 16),
            decoration: fieldDecoration(context).copyWith(counterText: ''),
          ),
          const SectionLabel('What is it for?'),
          TextField(
            controller: _mission,
            minLines: 3,
            maxLines: 6,
            style: TextStyle(color: p.text, fontSize: 16, height: 1.4),
            decoration: fieldDecoration(
              context,
              hint: 'e.g. Keep an eye on competitor pricing and tell me when it changes.',
            ),
          ),
          const SizedBox(height: 16),
          if (_error != null) ...[Notice(_error!), const SizedBox(height: 12)],
          PillButton(
            label: 'Create ${_name.text.trim().isEmpty ? 'bot' : _name.text.trim()}',
            busy: _busy,
            onPressed: _name.text.trim().isEmpty ? null : _create,
          ),
        ],
      ),
    );
  }
}

class _TemplateCard extends StatelessWidget {
  const _TemplateCard({required this.selected, required this.index, required this.onTap});
  final bool selected;
  final int index;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final t = botTemplates[index];
    return Material(
      color: selected ? p.raised : p.surface,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(Radii.lg),
        side: BorderSide(color: selected ? p.accent : p.line),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.lg),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              BotAvatar(
                botId: 'tpl-${t.preset}',
                appearance: presets[t.preset]!,
                size: 44,
                mood: selected ? 'happy' : 'idle',
              ),
              const Spacer(),
              Text(
                t.name,
                style: TextStyle(color: p.text, fontSize: 15.5, fontWeight: FontWeight.w700),
              ),
              const SizedBox(height: 2),
              Text(
                t.blurb,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(color: p.textDim, fontSize: 12.5, height: 1.3),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
