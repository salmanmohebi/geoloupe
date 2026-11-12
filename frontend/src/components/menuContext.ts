// Lets the content of a toolbar menu close it (e.g. after "Add error pane").

import { createContext, useContext } from "react";

export const MenuClose = createContext<() => void>(() => undefined);
export const useMenuClose = () => useContext(MenuClose);
