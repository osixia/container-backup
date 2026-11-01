package tests

import (
	"context"
	_ "embed"
	"fmt"
	"time"

	"dagger.io/dagger"

	"github.com/osixia/dagger-build/job"
)

// Run checks every architecture using only synthetic data, as the image user.
func Run(ctx context.Context, builds []*job.BuildResult) error {
	for _, build := range builds {
		for _, image := range build.Containers {
			if err := checkImage(ctx, image); err != nil {
				return err
			}
		}
	}
	return nil
}

func checkImage(ctx context.Context, image *dagger.Container) error {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()

	exec := image.
		WithNewFile("/tmp/backup-image-check.sh", imageCheck, dagger.ContainerWithNewFileOpts{Permissions: 0755}).
		WithExec([]string{"bash", "/tmp/backup-image-check.sh"}, dagger.ContainerWithExecOpts{Expect: dagger.ReturnTypeAny})

	code, err := exec.ExitCode(ctx)
	if err != nil {
		return err
	}
	if code == 0 {
		return nil
	}

	stdout, _ := exec.Stdout(ctx)
	stderr, _ := exec.Stderr(ctx)
	return fmt.Errorf("image checks: exit code %d:\n%s%s", code, stdout, stderr)
}

//go:embed testdata/image.sh
var imageCheck string
