/** Shared artwork from the approved Rapport homepage. All assets remain local. */
import type { CSSProperties } from "react";

export const ART_ROOT = "/atelier/";

export function BrandFilters() {
  return (
    <svg width="0" height="0" aria-hidden="true" className="brand-filters">
      <defs>
        <filter id="rapport-logo-paper" colorInterpolationFilters="sRGB">
          <feColorMatrix type="matrix" values="1 0 0 0 0 0 1 0 0 0 0 0 1 0 0 0 -3 0 0 2.85" result="ink" />
          <feComposite in="ink" in2="SourceGraphic" operator="in" />
        </filter>
        <filter id="rapport-art-paper" colorInterpolationFilters="sRGB">
          <feColorMatrix type="matrix" values="1 0 0 0 0 0 1 0 0 0 0 0 1 0 0 3 3 3 0 -2" />
        </filter>
      </defs>
    </svg>
  );
}

export function BrandMark({ size = 34, className = "" }: { size?: number; className?: string }) {
  return <span className={`rapport-mark ${className}`} style={{ width: size, height: size }} role="img" aria-label="心迹 Rapport" />;
}

export function BrandLogo({ className = "" }: { className?: string }) {
  return <span className={`brand-signature ${className}`} role="img" aria-label="心迹 Rapport"><span className="rapport-mark" aria-hidden="true" /><span className="rapport-wordmark" aria-hidden="true" /></span>;
}

export const ANIMALS = ["鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪"];
export const SIGNS = ["白羊座", "金牛座", "双子座", "巨蟹座", "狮子座", "处女座", "天秤座", "天蝎座", "射手座", "摩羯座", "水瓶座", "双鱼座"];
export type ArtKind = "animal" | "sign";
export const SPRITE_BOUNDS: Record<ArtKind, number[][]> = {
  animal: [[36,37,315,315],[389,33.5,315,315],[742,36,316,316],[1099,36,316,316],[37,376,317,317],[394,378.5,311,311],[745,376.5,316,316],[1101,376.5,313,313],[37,707.5,316,316],[390,708.5,315,315],[745,707.5,316,316],[1101,707.5,317,317]],
  sign: [[18.5,19,326,326],[382.5,22,326,326],[741,21,327,327],[1108,21,327,327],[14,366,330,330],[381.5,365,332,332],[740.5,366,332,332],[1106.5,363,333,333],[16.5,705,329,329],[386,704,329,329],[747,707,329,329],[1107,704,329,329]],
};

export function spriteStyle(kind: ArtKind, index: number): CSSProperties {
  const [x, y, width, height] = SPRITE_BOUNDS[kind][index % 12];
  return {
    backgroundImage: `url(${ART_ROOT}${kind === "animal" ? "chinese-zodiac-user.png" : "astrology-user.png"})`,
    backgroundSize: `${1448 / width * 100}% ${1086 / height * 100}%`,
    backgroundPosition: `${x / (1448 - width) * 100}% ${y / (1086 - height) * 100}%`,
  };
}

export function ZodiacArt({ kind, index, className = "" }: { kind: ArtKind; index: number; className?: string }) {
  return <span className={`watercolor-medallion ${className}`} style={spriteStyle(kind, index)} aria-hidden="true" />;
}
