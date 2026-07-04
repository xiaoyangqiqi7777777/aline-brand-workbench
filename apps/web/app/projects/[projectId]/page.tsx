import { ProjectWorkspace } from "@/features/projects/project-workspace";

type ProjectPageProps = {
  params: Promise<{
    projectId: string;
  }>;
};

export default async function ProjectPage({ params }: ProjectPageProps) {
  const { projectId } = await params;

  return <ProjectWorkspace initialProjectId={projectId} />;
}

