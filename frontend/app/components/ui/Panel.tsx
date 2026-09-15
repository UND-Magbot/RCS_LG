import { IconButton } from "./IconButton";
import type { PanelProps } from "@/lib/types/ui";

export function Panel({
  title,
  headerActions,
  headerContent,
  subheader,
  collapsed = false,
  collapsedLabel,
  collapsedTogglePosition,
  onToggle,
  toggleIcon = "left",
  className,
  footer,
  noBodyWrapper,
  children,
}: PanelProps) {
  const classes = ["panel", collapsed ? "panel--collapsed" : "", className]
    .filter(Boolean)
    .join(" ");

  if (collapsed) {
    const collapsedClasses = [
      "panel__collapsed",
      collapsedTogglePosition
        ? `panel__collapsed--${collapsedTogglePosition}`
        : "",
    ]
      .filter(Boolean)
      .join(" ");

    return (
      <aside className={classes}>
        <div className={collapsedClasses}>
          <IconButton
            aria-label={`Expand ${title}`}
            onClick={onToggle}
            className="panel__toggle"
            variant="ghost"
          >
            {toggleIcon === "right" ? ">" : "<"}
          </IconButton>
          {collapsedLabel ? (
            <div className="panel__collapsed-label">
              {collapsedLabel}
            </div>
          ) : null}
        </div>
      </aside>
    );
  }

  return (
    <aside className={classes}>
      <div className="panel__header">
        <div className="panel__header-row">
          <h2 className="panel__title">{title}</h2>
          {headerActions ? (
            <div className="panel__header-actions">{headerActions}</div>
          ) : null}
        </div>
        {headerContent}
      </div>
      {subheader ? <div className="panel__subheader">{subheader}</div> : null}
      {noBodyWrapper ? children : <div className="panel__body">{children}</div>}
      {footer ? <div className="panel__footer">{footer}</div> : null}
      {onToggle ? (
        <IconButton
          aria-label={`Collapse ${title}`}
          onClick={onToggle}
          className={`panel__toggle panel__toggle--edge panel__toggle--edge-${toggleIcon}`}
          variant="ghost"
        >
          {toggleIcon === "right" ? "<" : ">"}
        </IconButton>
      ) : null}
    </aside>
  );
}
