import { useState, useEffect, useCallback, useRef } from "react";
import Markdown from "react-markdown";
import type { StoryMap } from "../types";

interface Props {
  storyMap: StoryMap;
  onNavigate: (center: [number, number], zoom: number) => void;
  onHighlight: (layerIds: string[]) => void;
  onClose: () => void;
}

const MIN_WIDTH = 300;
const MAX_WIDTH = 600;
const MIN_HEIGHT = 200;
const MAX_HEIGHT = 600;

export default function StoryMapViewer({ storyMap, onNavigate, onHighlight, onClose }: Props) {
  const [currentStep, setCurrentStep] = useState(0);
  const [size, setSize] = useState({ width: 380, height: 360 });
  const dragging = useRef(false);
  const dragStart = useRef({ x: 0, y: 0, w: 0, h: 0 });
  const step = storyMap.steps[currentStep];
  const total = storyMap.steps.length;

  // Fly map and highlight layers when step changes
  useEffect(() => {
    if (!step) return;
    onNavigate([step.latitude, step.longitude], step.zoom);
    onHighlight(step.highlight_layers ?? []);
  }, [currentStep, step, onNavigate, onHighlight]);

  const resetResizeState = useCallback(() => {
    dragging.current = false;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }, []);

  // Clean up highlights when closing
  const handleClose = useCallback(() => {
    resetResizeState();
    onHighlight([]);
    onClose();
  }, [resetResizeState, onHighlight, onClose]);

  const goPrev = useCallback(() => setCurrentStep((s) => Math.max(0, s - 1)), []);
  const goNext = useCallback(() => setCurrentStep((s) => Math.min(total - 1, s + 1)), [total]);

  // Keyboard navigation
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target instanceof HTMLInputElement ||
          target instanceof HTMLTextAreaElement ||
          target instanceof HTMLSelectElement ||
          target.isContentEditable)
      ) {
        return;
      }

      if (e.key === "ArrowRight" || e.key === "ArrowDown") {
        e.preventDefault();
        goNext();
      } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
        e.preventDefault();
        goPrev();
      } else if (e.key === "Escape") {
        handleClose();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [goNext, goPrev, handleClose]);

  // Resize drag handlers
  const onResizeMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    dragStart.current = { x: e.clientX, y: e.clientY, w: size.width, h: size.height };
    document.body.style.cursor = "nwse-resize";
    document.body.style.userSelect = "none";
  }, [size]);

  useEffect(() => {
    const onMouseMove = (e: MouseEvent) => {
      if (!dragging.current) return;
      const dx = dragStart.current.x - e.clientX;
      const dy = dragStart.current.y - e.clientY;
      setSize({
        width: Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, dragStart.current.w + dx)),
        height: Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, dragStart.current.h + dy)),
      });
    };
    const onMouseUp = () => {
      if (!dragging.current) return;
      resetResizeState();
    };
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
      resetResizeState();
    };
  }, [resetResizeState]);

  if (!step) return null;

  return (
    <div className="story-map-overlay">
      <div className="story-map-panel" style={{ width: size.width, height: size.height }}>
        {/* Resize handle (top-left corner) */}
        <div className="story-map-resize" onMouseDown={onResizeMouseDown} title="Drag to resize" />

        {/* Header */}
        <div className="story-map-header">
          <div className="story-map-title">{storyMap.title}</div>
          <button className="story-map-close" onClick={handleClose} title="Close story map">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        {/* Progress */}
        <div className="story-map-progress">
          {storyMap.steps.map((_, i) => (
            <button
              key={i}
              className={`story-map-dot ${i === currentStep ? "story-map-dot--active" : ""} ${i < currentStep ? "story-map-dot--visited" : ""}`}
              onClick={() => setCurrentStep(i)}
              title={storyMap.steps[i].title}
              aria-label={`Go to step ${i + 1}: ${storyMap.steps[i].title}`}
            />
          ))}
        </div>

        {/* Content */}
        <div className="story-map-content">
          <h3 className="story-map-step-title">{step.title}</h3>
          <div className="story-map-narrative">
            <Markdown>{step.narrative}</Markdown>
          </div>
        </div>

        {/* Navigation */}
        <div className="story-map-nav">
          <button
            className="story-map-nav-btn"
            onClick={goPrev}
            disabled={currentStep === 0}
          >
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="15 18 9 12 15 6" />
            </svg>
            Prev
          </button>
          <span className="story-map-step-counter">
            {currentStep + 1} / {total}
          </span>
          <button
            className="story-map-nav-btn"
            onClick={goNext}
            disabled={currentStep === total - 1}
          >
            Next
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="9 18 15 12 9 6" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
}
