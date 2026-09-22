export type BusinessType = "Logistics" | "Manufacturing" | "Delivery" | "Warehouse";

export type DestinationOption = {
  id: string;
  name: string;
  floor: string;
};

export type CustomTaskStep = {
  id: string;
  num: number;
  destination: string;
  actionType: string;
  audio?: string;
  volume?: number;
};

export type CustomTask = {
  id: string;
  taskName: string;
  business: BusinessType;
  rcsTasks: boolean;
  returnTask: boolean;
  speed: number;
  steps: CustomTaskStep[];
  createTime: string;
};

export type CustomTaskFormState = {
  taskName: string;
  business: BusinessType | "";
  rcsTasks: boolean;
  returnTask: boolean;
  speed: number;
  steps: CustomTaskStep[];
};

export type StepFormState = {
  actionType: string;
  destination: string;
  audio: string;
  volume: string;
};

export type CustomTasksTableProps = {
  tasks: CustomTask[];
  onEdit: (taskId: string) => void;
  onDelete: (taskId: string) => void;
};

export type CustomTaskModalProps = {
  open: boolean;
  onClose: () => void;
  onSave: (task: CustomTask) => void;
  editTask: CustomTask | null;
};

export type AddStepModalProps = {
  open: boolean;
  onClose: () => void;
  onSave: (step: CustomTaskStep) => void;
  editStep: CustomTaskStep | null;
  nextNum: number;
};
