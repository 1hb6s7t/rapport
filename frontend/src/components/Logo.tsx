/** Existing public component API, using the approved shared watercolor brand. */
import { BrandMark } from "@/components/brand/Brand";

export function Logo({ size = 28, className = "" }: { size?: number; className?: string }) {
  return <BrandMark size={size} className={className} />;
}
