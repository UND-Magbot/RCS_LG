export type AlarmSeverity = "info" | "warning" | "error";

export type AlarmErrorType = "auth" | "task" | "robot" | "map" | "net";

export type AlarmListItemProps = {
  code: string;
  severity: AlarmSeverity;
  errorType: AlarmErrorType;
  timestamp: string;
  message: string;
  robotSn: string;
  onSpeak?: () => void;
  onRead?: () => void;
  onClick?: () => void;
};

export type AlarmDetailData = {
  id: string;
  code: string;
  severity: AlarmSeverity;
  errorType: AlarmErrorType;
  message: string;
  robotSn: string;
  occurredAt: string;
  clearedAt?: string;
};

export type AlarmDetailProps = {
  alarm: AlarmDetailData;
  onClose: () => void;
};

export type AlarmData = {
  id: string;
  code: string;
  severity: AlarmSeverity;
  errorType: AlarmErrorType;
  timestamp: string;
  message: string;
  robotSn: string;
  occurredAt: string;
  clearedAt?: string;
};

export type FilterType = "all" | AlarmSeverity;

export type TopBarProps = {
  dateTime: string;
  onToggleNav?: () => void;
  navExpanded?: boolean;
};

export type NavItem = {
  label: string;
  href: string;
  match?: string;
  icon: string;
};

export type SideNavProps = {
  items: NavItem[];
  collapsed?: boolean;
  onClose?: () => void;
  onItemSelect?: () => void;
};

export type UserDropdownProps = {
  userName: string;
  iconSrc?: string;

};
