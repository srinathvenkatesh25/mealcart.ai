import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Don't let the dev server write assistant-instruction files into the project.
  agentRules: false,
};

export default nextConfig;
