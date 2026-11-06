// Time navigation: step, jump, slider and animation. Playback advances only
// after the current frame's tiles have loaded, so frames are never skipped.

import { useEffect, useMemo, useRef, useState } from "react";
import { timelineOf } from "../lib/timeline";
import { useStore } from "../state/store";
import { Icon } from "./Icon";

const SPEEDS = [1500, 800, 400, 150];

export function TimeBar() {
  const main = useStore((s) => s.main);
  const loading = useStore((s) => s.loading);
  const { setTime, stepTime } = useStore.getState();
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [text, setText] = useState<string | null>(null);
  const shownAt = useRef(0);
  const tl = useMemo(() => timelineOf(main.layer), [main.layer]);
  const i = main.time ? tl.nearest(main.time) : -1;

  // playback: wait for tiles, then hold the frame for the chosen delay
  useEffect(() => {
    if (!playing) return;
    if (loading > 0) return;
    if (!shownAt.current) shownAt.current = performance.now();
    const wait = Math.max(0, SPEEDS[speed] - (performance.now() - shownAt.current));
    const t = setTimeout(() => {
      shownAt.current = 0;
      if (i >= tl.count - 1) setTime(tl.at(0));
      else stepTime(1);
    }, wait);
    return () => clearTimeout(t);
  }, [playing, loading, i, speed, tl, setTime, stepTime]);

  // keyboard: ← → step, shift = ×10 steps, space = play
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
      if (e.key === "ArrowRight") stepTime(e.shiftKey ? 10 : 1);
      else if (e.key === "ArrowLeft") stepTime(e.shiftKey ? -10 : -1);
      else if (e.key === " ") {
        e.preventDefault();
        setPlaying((p) => !p);
      } else return;
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [stepTime]);

  if (tl.count < 2) return null;

  const jump = (label: string) => {
    const want = label.trim();
    setTime(tl.at(tl.nearest(want)));
  };

  const yearStep = (() => {
    const step = main.layer?.time_step ?? "";
    if (step.includes("month")) return 12;
    if (step === "1 day") return 365;
    return null;
  })();

  return (
    <div className="time-bar panel" aria-label="Time">
      <div className="row gap center">
        {yearStep && (
          <button className="icon-btn" onClick={() => stepTime(-yearStep)} title="One year back">
            «
          </button>
        )}
        <button
          className="icon-btn"
          onClick={() => stepTime(-1)}
          title="Previous (←)"
          aria-label="Previous time"
        >
          ‹
        </button>
        <input
          className="time-input"
          value={text ?? main.time ?? ""}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => {
            if (text) jump(text);
            setText(null);
          }}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          aria-label="Time (ISO 8601)"
          title="Type a date (YYYY-MM-DD) and press Enter"
        />
        <button className="icon-btn" onClick={() => stepTime(1)} title="Next (→)" aria-label="Next time">
          ›
        </button>
        {yearStep && (
          <button className="icon-btn" onClick={() => stepTime(yearStep)} title="One year forward">
            »
          </button>
        )}
        <button
          className={`btn play ${playing ? "on" : ""}`}
          onClick={() => setPlaying(!playing)}
          title="Play (space)"
        >
          {playing ? (
            <>
              <Icon name="pause" size={12} /> Pause
            </>
          ) : (
            <>
              <Icon name="play" size={12} /> Play
            </>
          )}
        </button>
        <select
          value={speed}
          onChange={(e) => setSpeed(Number(e.target.value))}
          aria-label="Speed"
          className="speed"
        >
          {SPEEDS.map((_, k) => (
            <option key={k} value={k}>
              {["slow", "normal", "fast", "fastest"][k]}
            </option>
          ))}
        </select>
      </div>
      <input
        type="range"
        className="time-slider"
        min={0}
        max={tl.count - 1}
        value={Math.max(0, i)}
        onChange={(e) => setTime(tl.at(Number(e.target.value)))}
        aria-label="Time slider"
      />
      <div className="row between muted small">
        <span>{tl.at(0)}</span>
        <span>
          {i + 1} / {tl.count.toLocaleString()} · {main.layer?.time_step}
        </span>
        <span>{tl.at(tl.count - 1)}</span>
      </div>
    </div>
  );
}
