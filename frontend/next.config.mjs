/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // ESLint is not a dependency in this minimal POC; don't let it block builds.
  eslint: {
    ignoreDuringBuilds: true,
  },
};

export default nextConfig;
