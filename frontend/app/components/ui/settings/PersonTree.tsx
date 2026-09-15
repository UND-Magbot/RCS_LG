"use client";

import { useState, useMemo } from "react";
import type { BusinessGroup, BusinessUser } from "@/lib/types/settings";

interface PersonTreeProps {
  groups: BusinessGroup[];
  selectedUserId: number | null;
  onSelectUser: (user: BusinessUser) => void;
}

export function PersonTree({
  groups,
  selectedUserId,
  onSelectUser,
}: PersonTreeProps) {
  const [search, setSearch] = useState("");
  const [expandedIds, setExpandedIds] = useState<Set<string>>(
    () => new Set(groups.map((g) => g.id))
  );

  const filtered = useMemo(() => {
    if (!search) return groups;
    const kw = search.toLowerCase();
    return groups
      .map((g) => ({
        ...g,
        users: g.users.filter(
          (u) =>
            u.username.toLowerCase().includes(kw) ||
            u.loginId.toLowerCase().includes(kw) ||
            g.name.toLowerCase().includes(kw)
        ),
      }))
      .filter((g) => g.users.length > 0);
  }, [groups, search]);

  const toggleGroup = (id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="person-tree">
      <h3 className="person-tree__title">사용자 선택</h3>
      <input
        className="person-tree__search"
        type="text"
        placeholder="검색"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      <div className="person-tree__list">
        {filtered.map((group) => {
          const isExpanded = expandedIds.has(group.id);
          return (
            <div key={group.id} className="person-tree__group">
              <button
                className="person-tree__group-header"
                onClick={() => toggleGroup(group.id)}
              >
                <svg
                  className={`person-tree__arrow${isExpanded ? " person-tree__arrow--expanded" : ""}`}
                  width="12"
                  height="12"
                  viewBox="0 0 12 12"
                  fill="currentColor"
                >
                  <path d="M4 2l4 4-4 4" />
                </svg>
                <span>{group.name}</span>
                <span className="person-tree__count">{group.users.length}</span>
              </button>
              {isExpanded && (
                <ul className="person-tree__users">
                  {group.users.map((user) => (
                    <li key={user.id}>
                      <button
                        className={`person-tree__user${selectedUserId === user.id ? " person-tree__user--selected" : ""}`}
                        onClick={() => onSelectUser(user)}
                      >
                        {user.username}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
