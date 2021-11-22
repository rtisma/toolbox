package com.github.rtisma.toolbox.file_renamer.cli.command;

import java.io.File;
import java.util.List;
import java.util.concurrent.Callable;

import com.github.rtisma.toolbox.file_renamer.cli.util.VersionProvider;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import lombok.val;
import picocli.CommandLine;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;

import static com.github.rtisma.toolbox.file_renamer.Renamer.createRenamer;
import static java.util.Objects.isNull;
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

  @Option(
      names = {"-t", "--threads"},
      description = "Num threads",
      defaultValue = "4",
      required = true)
  private int threads;

  @Option(
      names = {"-s", "--search"},
      description = "Regex to match filenames against",
      required = true)
  private String regex;

  @Option(
      names = {"-r", "--recursive"},
      description = "Recursively search",
      required = true)
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
      try(val renamer = createRenamer(threads)){
        renamer.rename(paths, regex, recursive);
      }
    }
    return 0;
  }
}
