import "./globals.css";
import "./components/shell/shell.css";
import "./components/shell/alarm.css";

import type { Metadata } from "next";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "UND 관제 시스템",
  description: "UND 로봇 관제 시스템",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ko">
      <body suppressHydrationWarning>
        <Providers>
          {children}
        </Providers>
        <footer className="copyright">
          Copyrightⓒ 2026 UND Co., Ltd. All rights reserved
        </footer>
      </body>
    </html>
  );
}
