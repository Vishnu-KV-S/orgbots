"use client";

import { createContext, useContext } from "react";
import type { Bot, Me } from "@/lib/api/bots";

/**
 * Who is signed in, for a runtime with members — null without them, where the one
 * person may do everything. The server enforces every rule here (`domain/members.py`);
 * the UI only uses them to not offer what would be refused.
 */
export const MeContext = createContext<Me | null>(null);

export function useMe(): Me | null {
  return useContext(MeContext);
}

export function isAdmin(me: Me | null): boolean {
  return me === null || me.role === "owner" || me.role === "admin";
}

/** May `me` change this bot's setup? Its owner or an admin; anyone, for a bot nobody owns. */
export function canEdit(me: Me | null, bot: Pick<Bot, "owner_member_id">): boolean {
  return (
    me === null || bot.owner_member_id === null || bot.owner_member_id === me.id || isAdmin(me)
  );
}

/** A teammate's shared bot: usable, but its setup is theirs. */
export function isSharedWithMe(me: Me | null, bot: Bot): boolean {
  return me !== null && bot.visibility === "team" && bot.owner_member_id !== me.id;
}
