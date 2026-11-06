// Small inline SVG icons (no icon font: they render the same everywhere).

const PATHS = {
  down: "M4 6l4 4 4-4",
  up: "M4 10l4-4 4 4",
  right: "M6 4l4 4-4 4",
  left: "M10 4l-4 4 4 4",
  close: "M4 4l8 8M12 4l-8 8",
  menu: "M3 4.5h10M3 8h10M3 11.5h10",
  play: "M5 3.5v9l7.5-4.5z",
  pause: "M5 3.5v9M11 3.5v9",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 14, title }: { name: IconName; size?: number; title?: string }) {
  const filled = name === "play";
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined}
      style={{ verticalAlign: "-2px", flex: "none" }}
    >
      {title && <title>{title}</title>}
      <path
        d={PATHS[name]}
        fill={filled ? "currentColor" : "none"}
        stroke="currentColor"
        strokeWidth={name === "pause" ? 2.4 : 1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
