export function Logo({ className = "h-8 w-8" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden="true">
      <defs>
        <linearGradient id="lgLogo" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="oklch(0.85 0.18 165)" />
          <stop offset="100%" stopColor="oklch(0.7 0.18 220)" />
        </linearGradient>
      </defs>
      <path
        d="M6 4 L6 28 M26 4 L26 28 M6 10 C 14 10, 18 22, 26 22 M6 22 C 14 22, 18 10, 26 10"
        stroke="url(#lgLogo)"
        strokeWidth="2.4"
        strokeLinecap="round"
        fill="none"
      />
    </svg>
  );
}

export function LogoMark({ withWord = true }: { withWord?: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <Logo />
      {withWord && (
        <span className="font-display text-lg font-semibold tracking-tight">
          Helix<span className="text-primary">.</span>
        </span>
      )}
    </div>
  );
}
