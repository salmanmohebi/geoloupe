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
  splitRight: "M2.5 3.5h11v9h-11zM8 3.5v9",
  splitDown: "M2.5 3.5h11v9h-11zM2.5 8h11",
  expand: "M3 6V3h3M10 3h3v3M13 10v3h-3M6 13H3v-3",
  link: "M6.5 9.5l3-3M5.5 8L4 9.5a2.1 2.1 0 003 3L8.5 11M10.5 8L12 6.5a2.1 2.1 0 00-3-3L7.5 5",
  clock: "M8 2.5a5.5 5.5 0 110 11 5.5 5.5 0 010-11zM8 5v3.2l2.2 1.6",
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
