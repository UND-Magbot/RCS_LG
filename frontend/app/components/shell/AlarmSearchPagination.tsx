"use client";

import type { AlarmSearchPaginationProps } from "@/lib/types/alarm-search";

const PAGE_GROUP = 5;

export function AlarmSearchPagination({
  page,
  pageSize,
  total,
  onPageChange,
}: AlarmSearchPaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  const pageGroupStart =
    Math.floor((page - 1) / PAGE_GROUP) * PAGE_GROUP + 1;
  const pageNumbers = Array.from(
    { length: Math.min(PAGE_GROUP, totalPages - pageGroupStart + 1) },
    (_, i) => pageGroupStart + i
  );

  return (
    <div className="pagination">
      <button
        type="button"
        className="pagination__btn"
        disabled={page <= 1}
        onClick={() => onPageChange(page - 1)}
      >
        이전
      </button>
      {pageNumbers.map((num) => (
        <button
          key={num}
          type="button"
          className={`pagination__num${num === page ? " pagination__num--active" : ""}`}
          onClick={() => onPageChange(num)}
        >
          {num}
        </button>
      ))}
      <button
        type="button"
        className="pagination__btn"
        disabled={page >= totalPages}
        onClick={() => onPageChange(page + 1)}
      >
        다음
      </button>
      <span className="pagination__info">총 {total} 개</span>
    </div>
  );
}
