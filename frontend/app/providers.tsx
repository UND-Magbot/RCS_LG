"use client";

import { AlertProvider } from "@/lib/context/AlertContext";
import { GlobalAlertModal } from "./components/shell/GlobalAlertModal";
import type { ReactNode } from "react";

export function Providers({ children }: { children: ReactNode }) {
  return (
    <AlertProvider>
      {children}
      <GlobalAlertModal />
    </AlertProvider>
  );
}
