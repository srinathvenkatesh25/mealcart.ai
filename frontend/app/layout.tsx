import type { Metadata } from "next";
import { IBM_Plex_Mono, Libre_Franklin } from "next/font/google";
import "./globals.css";

// Franklin Gothic is the nutrition-label typeface; Libre Franklin is its open revival.
const franklin = Libre_Franklin({ variable: "--font-franklin", subsets: ["latin"], weight: ["400", "500", "600", "800", "900"] });
const plexMono = IBM_Plex_Mono({ variable: "--font-mono", subsets: ["latin"], weight: ["400", "500"] });

export const metadata: Metadata = {
  title: "MealCart",
  description: "Plan a macro-accurate week of meals and fill your Instacart cart.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${franklin.variable} ${plexMono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
