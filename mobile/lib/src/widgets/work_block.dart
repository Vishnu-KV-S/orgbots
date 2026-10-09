import 'package:flutter/material.dart';

import '../api.dart';
import '../text.dart';
import '../theme.dart';

/// Consecutive steps folded into one line: "Worked · 6 steps · 1 retried". While the
/// bot is on it the line shows its latest step and the last few steps stay open, so
/// you can watch it work; afterwards it collapses and opens on tap.
class WorkBlock extends StatefulWidget {
  const WorkBlock({super.key, required this.steps, required this.live});
  final List<BotMessage> steps;
  final bool live;

  @override
  State<WorkBlock> createState() => _WorkBlockState();
}

class _WorkBlockState extends State<WorkBlock> with SingleTickerProviderStateMixin {
  bool _open = false;
  late final AnimationController _shimmer = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1400),
  );

  @override
  void initState() {
    super.initState();
    if (widget.live) _shimmer.repeat(reverse: true);
  }

  @override
  void didUpdateWidget(WorkBlock old) {
    super.didUpdateWidget(old);
    if (widget.live && !_shimmer.isAnimating) _shimmer.repeat(reverse: true);
    if (!widget.live && _shimmer.isAnimating) _shimmer.stop();
  }

  @override
  void dispose() {
    _shimmer.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final steps = widget.steps;
    final live = widget.live;
    final expanded = _open || live;
    final failures = steps.where((s) => s.ok == false).length;
    final latest = describeAction(steps.last.action);
    final summary = [
      live ? (latest.isEmpty ? 'Working' : latest) : 'Worked',
      '${steps.length} step${steps.length == 1 ? '' : 's'}',
      if (failures > 0) '$failures retried',
    ].join(' · ');
    final shown = live && steps.length > 6 ? steps.sublist(steps.length - 6) : steps;

    return Container(
      decoration: BoxDecoration(
        color: p.surface,
        border: Border.all(color: p.line),
        borderRadius: BorderRadius.circular(Radii.md),
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          InkWell(
            onTap: () => setState(() => _open = !_open),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
              child: Row(
                children: [
                  if (live)
                    SizedBox.square(
                      dimension: 15,
                      child: CircularProgressIndicator(strokeWidth: 1.8, color: p.textDim),
                    )
                  else
                    Icon(Icons.check, size: 17, color: p.textDim),
                  const SizedBox(width: 10),
                  Expanded(
                    child: FadeTransition(
                      opacity: live
                          ? Tween(begin: 0.45, end: 1.0).animate(_shimmer)
                          : const AlwaysStoppedAnimation(1),
                      child: Text(
                        summary,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(color: p.textDim, fontSize: 13.5),
                      ),
                    ),
                  ),
                  Icon(
                    expanded ? Icons.expand_more : Icons.chevron_right,
                    size: 18,
                    color: p.textFaint,
                  ),
                ],
              ),
            ),
          ),
          AnimatedSize(
            duration: const Duration(milliseconds: 200),
            alignment: Alignment.topCenter,
            child: !expanded
                ? const SizedBox(width: double.infinity)
                : Padding(
                    padding: const EdgeInsets.fromLTRB(14, 0, 14, 12),
                    child: Column(spacing: 10, children: [for (final s in shown) _Step(step: s)]),
                  ),
          ),
        ],
      ),
    );
  }
}

class _Step extends StatelessWidget {
  const _Step({required this.step});
  final BotMessage step;

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final failed = step.ok == false;
    final what = describeAction(step.action);
    final detail = failed && step.error.isNotEmpty
        ? step.error
        : (step.actionType != 'plan' ? step.content : '');
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.only(top: 1),
          child: Icon(
            actionIcons[step.actionType] ?? Icons.chevron_right,
            size: 16,
            color: p.textFaint,
          ),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                what.isEmpty ? step.content : what,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  color: failed ? p.textFaint : p.text,
                  fontSize: 13.5,
                  height: 1.35,
                ),
              ),
              if (detail.isNotEmpty)
                Text(
                  detail,
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(color: p.textFaint, fontSize: 12, height: 1.35),
                ),
            ],
          ),
        ),
      ],
    );
  }
}
