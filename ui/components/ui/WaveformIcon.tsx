/**
 * A voice waveform — the mark chat apps put on their voice-mode button. Heroicons
 * has none, so this one is drawn to its 24px outline grid: the same 1.5 stroke,
 * round caps, and the `data-slot` that `styles/base.css` sizes icons by.
 */
export function WaveformIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={1.5}
      stroke="currentColor"
      aria-hidden="true"
      data-slot="icon"
      {...props}
    >
      <path
        strokeLinecap="round"
        d="M4.5 10.5v3M8.25 7.5v9M12 4.5v15M15.75 8.25v7.5M19.5 10.5v3"
      />
    </svg>
  );
}
