"use client";

interface LoadingScreenProps {
  pageName: string;
}

export function LoadingScreen({ pageName }: LoadingScreenProps) {
  return (
    <div className="loading-screen">
      <div className="loading-screen__content">
        <div className="loading-screen__spinner">
          <div className="loading-screen__spinner-ring" />
        </div>
        <div className="loading-screen__label">로딩 중...</div>
      </div>
    </div>
  );
}
