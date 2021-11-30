package com.github.rtisma.toolbox.file_renamer.cli.command;

import java.io.File;
import java.util.List;
import java.util.concurrent.Callable;

import com.github.rtisma.toolbox.file_renamer.cli.util.VersionProvider;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import lombok.val;
import picocli.CommandLine;
import picocli.CommandLine.ArgGroup;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;

import static com.github.rtisma.toolbox.file_renamer.Renamer.createRenamer;
import static java.util.Objects.isNull;
import static java.util.Objects.nonNull;
import static java.util.stream.Collectors.toUnmodifiableList;

@Slf4j
@RequiredArgsConstructor
@Command(
    name = "file-renamer",
    mixinStandardHelpOptions = true,
    versionProvider = VersionProvider.class,
    description = "Main command")
public class RootCommand implements Callable<Integer>
{

  @ArgGroup(exclusive = true, multiplicity = "1")
  private ThreadOptions threadOptions;

  @ArgGroup(exclusive = false)
  private DryRunOptions dryRunOptions;

  @Option(
      names = {"-s", "--search"},
      description = "Regex to match filenames against",
      required = true)
  private String regex;

  @Option(
      names = {"-p", "--collision-prefix"},
      description = "Prefix to use to mark colliding files",
      required = false,
      defaultValue = "md5" )
  private String collisionPrefix;

  @Option(
      names = {"-r", "--recursive"},
      description = "Recursively search",
      required = false)
  private boolean recursive;

  @Option(
      names = {"-f", "--files"},
      description = "Files to rename",
      required = false)
  private List<File> files;



  @Override
  public Integer call() throws Exception {
    if (isNull(files) || files.isEmpty()){
      CommandLine.usage(this, System.out);
    } else  {
      val paths = files.stream().map(File::toPath).collect(toUnmodifiableList());
      val resolvedThreads = threadOptions.resolveNumThreads();
      try(val renamer = createRenamer(resolvedThreads, collisionPrefix, dryRunOptions.dryRun, dryRunOptions.calcMd5)){
        renamer.rename(paths, regex, recursive);
      }
    }
    return 0;
  }

  static class ThreadOptions {
    @Option(
        names = {"-t", "--threads"},
        description = "Num threads",
        required = true)
    private Integer threads;

    @Option(
        names = {"-a", "--all-threads"},
        description = "All threads",
        required = true)
    private Boolean allThreads;

    private boolean isThreadsDefined() {
      return nonNull(threads);
    }

    private boolean isAllThreadsDefined() {
      return nonNull(allThreads);
    }

    public int resolveNumThreads() {
      if (isAllThreadsDefined()) {
        val availableThreads = Runtime.getRuntime().availableProcessors();
        return allThreads ? availableThreads : availableThreads/4;
      } else if (isThreadsDefined()) {

        return threads;
      } else {
        throw new IllegalStateException("should not be here");
      }

    }
  }

  static class DryRunOptions {
    @Option(
        names = {"-n", "--dry-run"},
        description = "Dry run to show which files are affected",
        required = false)
    private boolean dryRun;

    @Option(
        names = {"-m", "--md5"},
        description = "Also calculate md5",
        required = false)
    private boolean calcMd5;
  }

}
