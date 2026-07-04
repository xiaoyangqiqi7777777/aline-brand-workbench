import { EnvironmentStatus } from "@/components/environment-status";

const services = [
  ["统一入口", "http://localhost:8080"],
  ["网页直连", "http://localhost:3000"],
  ["API 文档", "http://localhost:8000/api/docs"],
  ["MinIO 控制台", "http://localhost:9001"],
] as const;

export default function Home() {
  return (
    <main className="shell">
      <section className="hero">
        <p className="eyebrow">ALINE · SHARED DEV</p>
        <h1>共同开发环境已就绪</h1>
        <p className="lead">
          这里是新的 Next.js 开发入口。根目录的 index.html 仍保留为原视觉参考，后续由前端负责人迁移。
        </p>
        <EnvironmentStatus />
      </section>

      <section className="grid" aria-label="开发服务入口">
        {services.map(([label, url]) => (
          <a className="card" href={url} key={label} rel="noreferrer" target="_blank">
            <strong>{label}</strong>
            <span>{url}</span>
          </a>
        ))}
      </section>

      <section className="notes">
        <h2>团队默认约定</h2>
        <ul>
          <li>默认使用假 AI，不需要任何模型密钥。</li>
          <li>共享假数据在 contracts/examples 目录。</li>
          <li>环境配置只写入本机 .env，不提交仓库。</li>
          <li>开始开发前先运行 make check。</li>
        </ul>
      </section>
    </main>
  );
}
