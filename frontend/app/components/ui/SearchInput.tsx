"use client";

import { useState, type KeyboardEvent } from "react";
import "./SearchInput.css";

type SearchInputProps = {
  placeholder?: string;
  onSearch: (keyword: string) => void;
  className?: string;
};

export function SearchInput({
  placeholder,
  onSearch,
  className,
}: SearchInputProps) {
  const [value, setValue] = useState("");

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      onSearch(value.trim());
    }
  };

  const handleSearchClick = () => {
    onSearch(value.trim());
  };

  return (
    <div className={`search-input${className ? ` ${className}` : ""}`}>
      <input
        className="search-input__field"
        placeholder={placeholder}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
      />
      <button
        className="search-input__btn"
        type="button"
        onClick={handleSearchClick}
        aria-label="Search"
      >
        <svg
          className="search-input__icon"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <circle cx="11" cy="11" r="8" />
          <line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
      </button>
    </div>
  );
}
