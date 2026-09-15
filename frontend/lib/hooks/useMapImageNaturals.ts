"use client";

import { useEffect, useState } from "react";

type ImageNaturals = { width: number; height: number } | null;

export function useMapImageNaturals(src: string): ImageNaturals {
  const [naturals, setNaturals] = useState<ImageNaturals>(null);

  useEffect(() => {
    if (!src) {
      setNaturals(null);
      return;
    }

    let cancelled = false;

    const img = new Image();

    img.onload = () => {
      if (!cancelled) {
        setNaturals({ width: img.naturalWidth, height: img.naturalHeight });
      }
    };

    img.onerror = () => {
      if (!cancelled) setNaturals(null);
    };

    img.src = src;

    return () => {
      cancelled = true;
    };
  }, [src]);

  return naturals;
}
