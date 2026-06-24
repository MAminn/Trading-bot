import { useQuery } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { checkIsAdmin } from "@/lib/admin.functions";

export function useIsAdmin() {
  const fn = useServerFn(checkIsAdmin);
  const q = useQuery({
    queryKey: ["admin", "check"],
    queryFn: () => fn({}),
    staleTime: 60_000,
  });
  return !!q.data?.isAdmin;
}
