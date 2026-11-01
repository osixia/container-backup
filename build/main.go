package main

import (
	"context"
	"os"

	"dagger.io/dagger"

	"github.com/osixia/container-backup/build/tests"
	"github.com/osixia/dagger-build/cmd"
	"github.com/osixia/dagger-build/config"
	"github.com/osixia/dagger-build/job"
)

func main() {
	image := &config.Image{
		BaseImage:     "alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a",
		Distribution:  config.Alpine,
		Name:          "osixia/backup",
		Description:   "Tools for application backups, archives and age encryption",
		Url:           "https://github.com/osixia/container-backup",
		Documentation: "https://github.com/osixia/container-backup",
		Source:        "https://github.com/osixia/container-backup",
		Authors:       "The osixia/container-backup maintainers",
		Vendor:        "Osixia",
		Licences:      "MIT",
	}

	config.Images = []*config.Image{image}
	config.DefaultImage = image
	config.ProjectGithubRepo = &config.GithubRepo{
		Organization: "osixia",
		Project:      "container-backup",
	}

	// The shared default test expects an entrypoint that reports the image tag.
	// This toolbox has no entrypoint, so run its functional checks instead.
	job.Test = func(ctx context.Context, client *dagger.Client, options any) ([]*job.BuildResult, error) {
		o := options.(*job.TestOptions)
		builds, err := job.Export(ctx, client, &o.ExportOptions)
		if err != nil {
			return nil, err
		}
		if err := tests.Run(ctx, builds); err != nil {
			return nil, err
		}
		return builds, nil
	}

	if err := cmd.Run(context.Background()); err != nil {
		os.Exit(1)
	}
}
