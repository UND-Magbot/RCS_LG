"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { apiFetch } from "@/lib/api";

export default function Home() {
  const router = useRouter();

  useEffect(() => {
    const token = localStorage.getItem("auth_token");

    if (!token) {
      router.replace("/auth/login");
      return;
    }

    apiFetch("/api/auth/me", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(() => {
        router.replace("/monitoring");
      })
      .catch(() => {
        localStorage.removeItem("auth_token");
        localStorage.removeItem("user_login_id");
        localStorage.removeItem("user_role");
        router.replace("/auth/login");
      });
  }, [router]);

  return null;
}
