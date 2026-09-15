import type { ButtonHTMLAttributes, ReactNode } from "react";

export type PanelProps = {
  title: string;
  headerActions?: ReactNode;
  headerContent?: ReactNode;
  subheader?: ReactNode;
  collapsed?: boolean;
  collapsedLabel?: string;
  collapsedTogglePosition?: "start" | "end";
  onToggle?: () => void;
  toggleIcon?: "left" | "right";
  className?: string;
  footer?: ReactNode;
  noBodyWrapper?: boolean;
  children?: ReactNode;
};

export type IconButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "default" | "ghost";
};

export type ModalProps = {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  width?: string;
  height?: string;
};
