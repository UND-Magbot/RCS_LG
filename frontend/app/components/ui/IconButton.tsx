import type { IconButtonProps } from "@/lib/types/ui";

export function IconButton({
  className,
  variant = "default",
  ...rest
}: IconButtonProps) {
  const classes = [
    "icon-btn",
    variant === "ghost" ? "icon-btn--ghost" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return <button className={classes} {...rest} />;
}
